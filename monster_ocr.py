"""Start the legacy OCR or combat entry point."""

from wow_assistant.geometry import enable_dpi_awareness, region_on_screen, valid_region
from wow_assistant.tools.ocr import App, OCRWorker, load_config, main, match_target, normalize, save_config
from wow_assistant.ui.selection import select_screen_region


if __name__ == "__main__":
    raise SystemExit(main())
