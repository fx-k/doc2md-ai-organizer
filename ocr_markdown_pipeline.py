#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


MIN_PYTHON = (3, 10)
OUTPUT_PREFIX = "👌🤖"
STATE_DIR_NAME = "._ocr_markdown_pipeline"
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OCR_SCRIPT = PROJECT_DIR / "examples" / "ocr_adapter.py"
DEFAULT_DOWNLOADS_DIR = Path.home() / "Downloads"
DEFAULT_AI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_AI_MODEL = "gpt-4o-mini"
LEGACY_OFFICE_TARGETS = {
    ".doc": "docx",
    ".dot": "docx",
    ".ppt": "pptx",
    ".pps": "pptx",
    ".pot": "pptx",
    ".xls": "xlsx",
    ".xlt": "xlsx",
}
SOFFICE_CANDIDATES = [
    Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
    Path("/opt/homebrew/bin/soffice"),
    Path("/usr/local/bin/soffice"),
]


class PipelineError(RuntimeError):
    pass


def ensure_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        required = ".".join(str(part) for part in MIN_PYTHON)
        current = ".".join(str(part) for part in sys.version_info[:3])
        raise PipelineError(
            f"Python {required}+ is required. Current Python version is {current}. "
            "Create a virtual environment with python3.10 or newer."
        )


@dataclass(frozen=True)
class Config:
    input_dir: Path
    output_dir: Path
    state_dir: Path
    office_converted_dir: Path
    converted_dir: Path
    renamed_dir: Path
    soffice: Path | None
    office_timeout_seconds: int
    ocr_python: Path
    ocr_script: Path
    ocr_cwd: Path
    retries: int
    retry_wait_seconds: float
    ocr_timeout_seconds: int
    ai_base_url: str
    ai_model: str
    ai_api_key: str
    ai_timeout_seconds: int
    include_hidden: bool
    dry_run: bool


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sanitize_filename(value: str, fallback: str = "untitled", max_len: int = 80) -> str:
    value = value.strip()
    value = re.sub(r"[\r\n\t]+", " ", value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" ._-")
    if not value:
        value = fallback
    if len(value) > max_len:
        value = value[:max_len].rstrip(" ._-")
    return value or fallback


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    for index in range(2, 10000):
        candidate = parent / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise PipelineError(f"Cannot create a unique path for {path}")


def source_hash(relative_path: Path) -> str:
    return hashlib.sha1(str(relative_path).encode("utf-8")).hexdigest()[:10]


def flat_markdown_name(relative_path: Path) -> str:
    parts = [p for p in relative_path.with_suffix("").parts if p]
    base = sanitize_filename("__".join(parts), fallback="document", max_len=70)
    return f"{base}__{source_hash(relative_path)}.md"


def legacy_office_target(source: Path) -> str | None:
    return LEGACY_OFFICE_TARGETS.get(source.suffix.lower())


def office_conversion_dir(base_dir: Path, relative_path: Path) -> Path:
    parts = [p for p in relative_path.with_suffix("").parts if p]
    base = sanitize_filename("__".join(parts), fallback="office-document", max_len=70)
    return base_dir / f"{base}__{source_hash(relative_path)}"


def resolve_soffice(value: Path | None) -> Path | None:
    if value:
        return value.expanduser().resolve()
    from_path = shutil.which("soffice") or shutil.which("libreoffice")
    if from_path:
        return Path(from_path).resolve()
    for candidate in SOFFICE_CANDIDATES:
        if candidate.exists():
            return candidate.resolve()
    return None


def executable_path(value: Path) -> Path:
    path = value.expanduser()
    if path.is_absolute():
        return path
    return path.absolute()


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "files": {},
            "organization": {},
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = now_iso()
    atomic_write_json(path, manifest)


def append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"time": now_iso(), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def setup_logging(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / "run.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
        force=True,
    )


def discover_files(input_dir: Path, include_hidden: bool) -> list[Path]:
    files: list[Path] = []
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(input_dir)
        if path.name == ".DS_Store":
            continue
        if path.name.startswith("~$"):
            continue
        if not include_hidden and any(part.startswith(".") for part in rel.parts):
            continue
        files.append(path)
    return files


def retry(
    label: str,
    retries: int,
    wait_seconds: float,
    errors_path: Path,
    func,
) -> Any:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return func(attempt)
        except Exception as exc:  # noqa: BLE001 - all failures are logged for retry.
            last_exc = exc
            append_jsonl(
                errors_path,
                {
                    "label": label,
                    "attempt": attempt,
                    "max_attempts": retries,
                    "error": repr(exc),
                },
            )
            logging.exception("%s failed on attempt %s/%s", label, attempt, retries)
            if attempt < retries:
                sleep_for = wait_seconds * attempt
                logging.info("Retrying %s after %.1f seconds", label, sleep_for)
                time.sleep(sleep_for)
    raise PipelineError(f"{label} failed after {retries} attempts: {last_exc}") from last_exc


def prepare_ocr_source(
    source: Path,
    source_rel: Path,
    config: Config,
    manifest_record: dict[str, Any],
    errors_path: Path,
) -> Path:
    target_ext = legacy_office_target(source)
    if not target_ext:
        manifest_record.pop("office_converted_path", None)
        return source

    existing = manifest_record.get("office_converted_path")
    if existing and Path(existing).exists():
        logging.info("Office conversion skip existing: %s", Path(existing).name)
        return Path(existing)

    if not config.soffice:
        raise PipelineError(
            "Legacy Office file needs LibreOffice/soffice before OCR, but soffice was not found. "
            "Install LibreOffice or pass --soffice /path/to/soffice."
        )
    if not config.soffice.exists():
        raise PipelineError(f"Configured soffice does not exist: {config.soffice}")

    out_dir = office_conversion_dir(config.office_converted_dir, source_rel)
    expected = out_dir / f"{source.stem}.{target_ext}"

    def run(_: int) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        lo_profile_dir = config.state_dir / "libreoffice_profile"
        lo_profile_dir.mkdir(parents=True, exist_ok=True)
        for old_output in out_dir.glob(f"*.{target_ext}"):
            old_output.unlink()

        command = [
            str(config.soffice),
            f"-env:UserInstallation={lo_profile_dir.as_uri()}",
            "--headless",
            "--convert-to",
            target_ext,
            "--outdir",
            str(out_dir),
            str(source),
        ]
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=config.office_timeout_seconds or None,
            check=False,
        )
        if completed.returncode != 0:
            raise PipelineError(
                f"Office conversion failed with exit code {completed.returncode}: "
                f"{(completed.stderr or completed.stdout)[-4000:]}"
            )

        if expected.exists():
            return expected
        candidates = sorted(out_dir.glob(f"*.{target_ext}"), key=lambda item: item.stat().st_mtime)
        if not candidates:
            raise PipelineError(
                "Office conversion finished but no converted file was created. "
                f"stdout={completed.stdout[-2000:]} stderr={completed.stderr[-2000:]}"
            )
        return candidates[-1]

    converted = retry(
        f"office-convert:{source_rel}",
        config.retries,
        config.retry_wait_seconds,
        errors_path,
        run,
    )
    manifest_record["office_converted_path"] = str(converted)
    manifest_record["office_converted_from"] = str(source)
    logging.info("Office converted: %s -> %s", source_rel, converted.name)
    return converted


def convert_one_file(source: Path, dest: Path, config: Config, errors_path: Path) -> None:
    if dest.exists():
        logging.info("OCR skip existing: %s", dest.name)
        return

    def run(_: int) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        if tmp.exists():
            tmp.unlink()

        if source.suffix.lower() == ".md":
            shutil.copy2(source, tmp)
            tmp.replace(dest)
            return

        env = os.environ.copy()
        python_bin = config.ocr_python.parent
        venv_dir = python_bin.parent
        if (venv_dir / "pyvenv.cfg").exists():
            env["VIRTUAL_ENV"] = str(venv_dir)
        env["PATH"] = f"{python_bin}{os.pathsep}{env.get('PATH', '')}"

        command = [str(config.ocr_python), str(config.ocr_script), str(source)]
        timeout = config.ocr_timeout_seconds or None
        with tmp.open("w", encoding="utf-8") as stdout_handle:
            completed = subprocess.run(
                command,
                cwd=config.ocr_cwd,
                env=env,
                stdout=stdout_handle,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
                check=False,
            )

        if completed.returncode != 0:
            if tmp.exists():
                tmp.unlink()
            raise PipelineError(
                f"OCR command failed with exit code {completed.returncode}: "
                f"{completed.stderr[-4000:]}"
            )

        if not tmp.exists():
            raise PipelineError("OCR command finished but did not create markdown output")
        if tmp.stat().st_size == 0:
            logging.warning("OCR produced an empty markdown file: %s", source)
        tmp.replace(dest)

    retry(f"ocr:{source}", config.retries, config.retry_wait_seconds, errors_path, run)


def read_snippet(path: Path, limit: int = 5000) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise PipelineError(f"AI response did not contain a JSON object: {text[:500]}")
    return json.loads(text[start : end + 1])


def ai_json(config: Config, system_prompt: str, user_prompt: str) -> dict[str, Any]:
    url = config.ai_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.ai_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    if config.ai_api_key:
        headers["Authorization"] = f"Bearer {config.ai_api_key}"

    def post(body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=config.ai_timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise PipelineError(f"AI HTTP {exc.code}: {error_body[:2000]}") from exc
        data = json.loads(raw)
        content = data["choices"][0]["message"]["content"]
        return parse_json_object(content)

    try:
        return post(payload)
    except PipelineError as exc:
        if "response_format" not in repr(exc):
            raise
        payload.pop("response_format", None)
        return post(payload)


def rename_one_file(
    source_rel: Path,
    converted_path: Path,
    config: Config,
    manifest_record: dict[str, Any],
    errors_path: Path,
) -> Path:
    existing = manifest_record.get("renamed_path")
    if existing and Path(existing).exists():
        logging.info("Rename skip existing: %s", Path(existing).name)
        return Path(existing)

    system_prompt = (
        "你是中文资料库文件命名助手。目标是让文件名方便 AI 检索。"
        "只输出合法 JSON，格式为 {\"title\":\"...\"}。"
        "标题要使用简体中文，保留关键法规、标准、品类、年份、场景和用途；"
        "不要包含扩展名，不要包含斜杠、冒号、换行或引号；不要编造文档中没有的信息。"
    )
    snippet = read_snippet(converted_path)
    user_prompt = (
        "重命名文件标题，给出一个恰当的标题，方便ai检索的。\n\n"
        f"原文件相对路径：{source_rel}\n"
        f"原文件名：{source_rel.name}\n"
        "OCR 后 Markdown 内容片段：\n"
        f"{snippet}"
    )

    def run(_: int) -> str:
        result = ai_json(config, system_prompt, user_prompt)
        title = str(result.get("title", "")).strip()
        if not title:
            raise PipelineError(f"AI rename result has no title: {result}")
        return title

    title = retry(
        f"ai-rename:{source_rel}",
        config.retries,
        config.retry_wait_seconds,
        errors_path,
        run,
    )
    filename = sanitize_filename(title, fallback=converted_path.stem, max_len=90) + ".md"
    dest = unique_path(config.renamed_dir / filename)
    config.renamed_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(converted_path, dest)
    manifest_record["ai_title"] = title
    manifest_record["renamed_path"] = str(dest)
    logging.info("Renamed: %s -> %s", source_rel, dest.name)
    return dest


def build_organization_plan(
    root_name: str,
    files: list[dict[str, str]],
    config: Config,
    errors_path: Path,
) -> dict[str, Any]:
    system_prompt = (
        "你是中文资料库目录整理助手。你只输出合法 JSON。"
        "你的任务是把 markdown 文件归入若干个二级文件夹；一级目录名称必须保持不变。"
        "二级目录名称要短、清晰、方便 AI 检索，不要使用斜杠。"
    )
    user_prompt = (
        f"一级目录名称：{root_name}\n"
        "一级目录不允许改变，只能在这个目录下面创建若干二级文件夹。\n"
        "请把下面所有 markdown 文件各归入且只归入一个二级文件夹。\n\n"
        "输出 JSON 格式必须是：\n"
        "{\n"
        "  \"directories\": [{\"name\": \"二级目录名\", \"reason\": \"归类理由\"}],\n"
        "  \"placements\": [{\"file\": \"文件名.md\", \"directory\": \"二级目录名\"}]\n"
        "}\n\n"
        "文件列表：\n"
        f"{json.dumps(files, ensure_ascii=False, indent=2)}"
    )

    def run(_: int) -> dict[str, Any]:
        result = ai_json(config, system_prompt, user_prompt)
        if not isinstance(result.get("placements"), list):
            raise PipelineError(f"AI organization result has no placements: {result}")
        return result

    return retry(
        "ai-organize",
        config.retries,
        config.retry_wait_seconds,
        errors_path,
        run,
    )


def normalize_plan(plan: dict[str, Any], renamed_paths: list[Path]) -> dict[str, str]:
    known_files = {path.name for path in renamed_paths}
    directories: set[str] = set()
    for item in plan.get("directories", []):
        if isinstance(item, dict):
            name = sanitize_filename(str(item.get("name", "")), fallback="其他资料", max_len=40)
            directories.add(name)

    placements: dict[str, str] = {}
    for item in plan.get("placements", []):
        if not isinstance(item, dict):
            continue
        filename = str(item.get("file", "")).strip()
        if filename not in known_files:
            continue
        directory = sanitize_filename(str(item.get("directory", "")), fallback="其他资料", max_len=40)
        directories.add(directory)
        placements[filename] = directory

    if not directories:
        directories.add("其他资料")
    for filename in known_files:
        placements.setdefault(filename, "其他资料")
    return placements


def organize_files(
    config: Config,
    manifest: dict[str, Any],
    manifest_path: Path,
    root_name: str,
    records: list[tuple[Path, dict[str, Any]]],
    errors_path: Path,
) -> None:
    renamed_paths = [Path(record["renamed_path"]) for _, record in records]
    org_files = [
        {
            "file": path.name,
            "source": str(source_rel),
            "ai_title": str(record.get("ai_title", "")),
        }
        for (source_rel, record), path in zip(records, renamed_paths)
    ]
    plan = build_organization_plan(root_name, org_files, config, errors_path)
    placements = normalize_plan(plan, renamed_paths)
    manifest["organization"] = {
        "planned_at": now_iso(),
        "raw_plan": plan,
        "placements": placements,
    }

    for source_rel, record in records:
        renamed_path = Path(record["renamed_path"])
        category = placements.get(renamed_path.name, "其他资料")
        category_dir = config.output_dir / category
        category_dir.mkdir(parents=True, exist_ok=True)

        existing_final = record.get("final_path")
        if existing_final and Path(existing_final).exists():
            logging.info("Organize skip existing: %s", existing_final)
            continue

        dest = unique_path(category_dir / renamed_path.name)
        shutil.copy2(renamed_path, dest)
        record["category"] = category
        record["final_path"] = str(dest)
        record["status"] = "done"
        logging.info("Organized: %s -> %s/%s", renamed_path.name, category, dest.name)
        save_manifest(manifest_path, manifest)


def validate_config(config: Config) -> None:
    if not config.input_dir.exists() or not config.input_dir.is_dir():
        raise PipelineError(f"Input folder does not exist or is not a directory: {config.input_dir}")
    if not config.ocr_python.exists():
        raise PipelineError(f"OCR python not found: {config.ocr_python}")
    if not config.ocr_script.exists():
        raise PipelineError(f"OCR script not found: {config.ocr_script}")
    if config.soffice and not config.soffice.exists():
        raise PipelineError(f"Configured soffice does not exist: {config.soffice}")
    if config.output_dir.resolve() == config.input_dir.resolve():
        raise PipelineError("Output folder cannot be the same as input folder")


def make_config(args: argparse.Namespace) -> Config:
    input_dir = args.folder.expanduser().resolve()
    output_parent = args.output_parent.expanduser().resolve() if args.output_parent else input_dir.parent
    output_dir = output_parent / f"{OUTPUT_PREFIX}{input_dir.name}"
    if args.force_new_output and output_dir.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_dir = output_parent / f"{OUTPUT_PREFIX}{input_dir.name}-{stamp}"

    ocr_python = executable_path(args.ocr_python) if args.ocr_python else Path(sys.executable)
    ocr_script = args.ocr_script.expanduser().resolve() if args.ocr_script else DEFAULT_OCR_SCRIPT
    soffice = resolve_soffice(args.soffice)
    state_dir = output_dir / STATE_DIR_NAME
    return Config(
        input_dir=input_dir,
        output_dir=output_dir,
        state_dir=state_dir,
        office_converted_dir=state_dir / "office_converted",
        converted_dir=state_dir / "converted_flat",
        renamed_dir=state_dir / "renamed_flat",
        soffice=soffice,
        office_timeout_seconds=args.office_timeout,
        ocr_python=ocr_python,
        ocr_script=ocr_script,
        ocr_cwd=args.ocr_cwd.expanduser().resolve(),
        retries=args.retries,
        retry_wait_seconds=args.retry_wait,
        ocr_timeout_seconds=args.ocr_timeout,
        ai_base_url=args.ai_base_url,
        ai_model=args.ai_model,
        ai_api_key=args.ai_api_key,
        ai_timeout_seconds=args.ai_timeout,
        include_hidden=args.include_hidden,
        dry_run=args.dry_run,
    )


def run_pipeline(config: Config) -> None:
    validate_config(config)
    files = discover_files(config.input_dir, config.include_hidden)
    if not files:
        raise PipelineError(f"No files found in input folder: {config.input_dir}")

    if config.dry_run:
        legacy_count = sum(1 for path in files if legacy_office_target(path))
        print(f"Input:  {config.input_dir}")
        print(f"Output: {config.output_dir}")
        print(f"Files:  {len(files)}")
        print(f"Legacy Office files needing pre-conversion: {legacy_count}")
        print(f"OCR python: {config.ocr_python}")
        print(f"OCR script: {config.ocr_script}")
        print(f"soffice: {config.soffice or 'not found'}")
        return

    config.output_dir.mkdir(parents=True, exist_ok=True)
    config.office_converted_dir.mkdir(parents=True, exist_ok=True)
    config.converted_dir.mkdir(parents=True, exist_ok=True)
    config.renamed_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(config.state_dir)

    manifest_path = config.state_dir / "manifest.json"
    errors_path = config.state_dir / "errors.jsonl"
    manifest = load_manifest(manifest_path)
    manifest["input_dir"] = str(config.input_dir)
    manifest["output_dir"] = str(config.output_dir)
    manifest["ocr_python"] = str(config.ocr_python)
    manifest["ocr_script"] = str(config.ocr_script)
    manifest["soffice"] = str(config.soffice) if config.soffice else None
    manifest["ai_base_url"] = config.ai_base_url
    manifest["ai_model"] = config.ai_model
    save_manifest(manifest_path, manifest)

    logging.info("Input folder: %s", config.input_dir)
    logging.info("Output folder: %s", config.output_dir)
    logging.info("OCR python: %s", config.ocr_python)
    logging.info("OCR script: %s", config.ocr_script)
    logging.info("soffice: %s", config.soffice or "not found")
    logging.info("Discovered %s file(s)", len(files))

    records: list[tuple[Path, dict[str, Any]]] = []
    failures: list[dict[str, str]] = []

    for index, source in enumerate(files, start=1):
        source_rel = source.relative_to(config.input_dir)
        key = str(source_rel)
        record = manifest["files"].setdefault(
            key,
            {
                "source_path": str(source),
                "source_relative_path": key,
                "status": "pending",
            },
        )
        converted_path = config.converted_dir / flat_markdown_name(source_rel)
        record["converted_path"] = str(converted_path)
        logging.info("Prepare %s/%s: %s", index, len(files), source_rel)
        try:
            ocr_source = prepare_ocr_source(source, source_rel, config, record, errors_path)
            record["ocr_input_path"] = str(ocr_source)
            if ocr_source != source:
                record["status"] = "office_converted"
                save_manifest(manifest_path, manifest)
        except Exception as exc:  # noqa: BLE001
            record["status"] = "office_convert_failed"
            record["error"] = repr(exc)
            failures.append({"file": key, "stage": "office_convert", "error": repr(exc)})
            save_manifest(manifest_path, manifest)
            continue

        logging.info("OCR %s/%s: %s", index, len(files), source_rel)
        try:
            convert_one_file(ocr_source, converted_path, config, errors_path)
            record["status"] = "converted"
            save_manifest(manifest_path, manifest)
            records.append((source_rel, record))
        except Exception as exc:  # noqa: BLE001
            record["status"] = "ocr_failed"
            record["error"] = repr(exc)
            failures.append({"file": key, "stage": "ocr", "error": repr(exc)})
            save_manifest(manifest_path, manifest)

    if failures:
        failed_path = config.state_dir / "failed_files.json"
        atomic_write_json(failed_path, failures)
        raise PipelineError(
            f"{len(failures)} file(s) failed during Office/OCR conversion after retries. See {failed_path}"
        )

    for index, (source_rel, record) in enumerate(records, start=1):
        logging.info("AI rename %s/%s: %s", index, len(records), source_rel)
        converted_path = Path(record["converted_path"])
        try:
            rename_one_file(source_rel, converted_path, config, record, errors_path)
            record["status"] = "renamed"
            save_manifest(manifest_path, manifest)
        except Exception as exc:  # noqa: BLE001
            record["status"] = "rename_failed"
            record["error"] = repr(exc)
            save_manifest(manifest_path, manifest)
            raise

    logging.info("AI organize: %s markdown file(s)", len(records))
    organize_files(
        config=config,
        manifest=manifest,
        manifest_path=manifest_path,
        root_name=config.input_dir.name,
        records=records,
        errors_path=errors_path,
    )

    final_files = [
        Path(record["final_path"])
        for _, record in records
        if record.get("final_path") and Path(record["final_path"]).exists()
    ]
    if len(final_files) != len(files):
        raise PipelineError(
            f"Final validation failed: expected {len(files)} markdown files, got {len(final_files)}"
        )

    summary = {
        "completed_at": now_iso(),
        "input_dir": str(config.input_dir),
        "output_dir": str(config.output_dir),
        "file_count": len(final_files),
        "categories": sorted({path.parent.name for path in final_files}),
    }
    atomic_write_json(config.state_dir / "summary.json", summary)
    logging.info("Done. Converted %s file(s).", len(final_files))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert every file in a folder to markdown with an OCR adapter, "
            "AI-rename the markdown files, then AI-organize them into second-level folders."
        )
    )
    parser.add_argument("folder", type=Path, help="Input folder. Original files are never modified.")
    parser.add_argument("--output-parent", type=Path, help="Where to create the prefixed output folder.")
    parser.add_argument("--force-new-output", action="store_true", help="Create a timestamped output folder if the default output exists.")
    parser.add_argument("--soffice", type=Path, help="Path to LibreOffice soffice. Auto-detected when omitted.")
    parser.add_argument("--office-timeout", type=int, default=900, help="Seconds before killing one legacy Office conversion. 0 means no timeout.")
    parser.add_argument("--ocr-python", type=Path)
    parser.add_argument("--ocr-script", type=Path)
    parser.add_argument("--ocr-cwd", type=Path, default=DEFAULT_DOWNLOADS_DIR)
    parser.add_argument("--ocr-timeout", type=int, default=0, help="Seconds before killing one OCR command. 0 means no timeout.")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-wait", type=float, default=10.0)
    parser.add_argument("--ai-base-url", default=os.getenv("AI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or DEFAULT_AI_BASE_URL)
    parser.add_argument("--ai-model", default=os.getenv("AI_MODEL") or os.getenv("OPENAI_MODEL") or DEFAULT_AI_MODEL)
    parser.add_argument("--ai-api-key", default=os.getenv("AI_API_KEY") or os.getenv("OPENAI_API_KEY") or "")
    parser.add_argument("--ai-timeout", type=int, default=300)
    parser.add_argument("--include-hidden", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    try:
        ensure_python_version()
        args = parse_args(argv)
        config = make_config(args)
        run_pipeline(config)
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Re-run the same command to resume from the saved state.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"Failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
