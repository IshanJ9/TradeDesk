r"""One-time download of the local speech-to-text model (faster-whisper), so VOICE_PROVIDER=local works offline.
The app itself never downloads anything: if the files are missing, local voice reports "not set up".

    .venv\Scripts\python -m pip install -r requirements-voice-local.txt
    .venv\Scripts\python scripts\download_voice_model.py            # small (~480 MB) into LOCAL_VOICE_DIR
    .venv\Scripts\python scripts\download_voice_model.py --size base

Then set VOICE_PROVIDER=local in .env (and LOCAL_VOICE_DIR if you chose another size or folder).
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402

SIZES = {"base": "Systran/faster-whisper-base", "small": "Systran/faster-whisper-small",
         "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--size", choices=sorted(SIZES), default="small")
    parser.add_argument("--dir", help="where to put it (default: LOCAL_VOICE_DIR from .env)")
    args = parser.parse_args()
    target = Path(args.dir or Settings.from_env().local_voice_dir)
    if not target.is_absolute():
        target = ROOT / target
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("Install the optional package first: pip install -r requirements-voice-local.txt")
        return 1
    print(f"Downloading {SIZES[args.size]} into {target} ...")
    snapshot_download(repo_id=SIZES[args.size], local_dir=str(target),
                      allow_patterns=["model.bin", "config.json", "tokenizer.json", "vocabulary.*", "preprocessor_config.json"])
    if not (target / "model.bin").is_file():
        print("Download finished but model.bin is missing.")
        return 1
    size_mb = sum(f.stat().st_size for f in target.rglob("*") if f.is_file()) / 1e6
    print(f"Done: {size_mb:,.0f} MB. Set VOICE_PROVIDER=local in .env to use it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
