"""不依赖 OCR 模型的逻辑与线程回归测试。"""

import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import monster_ocr as app


@contextmanager
def config_file():
    # 只清理这一个明确的临时文件，不递归删除临时目录。
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        path = Path(handle.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


class LogicTests(unittest.TestCase):
    def test_exact_and_normalized_contains(self):
        self.assertEqual(app.match_target("精英 腐皮 食尸鬼！", "腐皮食尸鬼"), (True, 1))
        self.assertEqual(app.match_target(" ＷＯＬＦ Lv10", "wolf"), (True, 1))

    def test_fuzzy_substitution_and_missing_character(self):
        self.assertTrue(app.match_target("精英腐皮食屍鬼", "腐皮食尸鬼")[0])
        self.assertTrue(app.match_target("腐皮尸鬼", "腐皮食尸鬼")[0])
        self.assertFalse(app.match_target("腐皮食屍鬼", "腐皮食尸鬼", 0.95)[0])

    def test_empty_short_and_unrelated(self):
        for text, target in (("", "食尸鬼"), ("食尸鬼", ""), ("野猪", "野狼"),
                             ("森林野狼", "腐皮食尸鬼"), ("!!!", "!!!")):
            self.assertFalse(app.match_target(text, target)[0])

    def test_negative_monitor_and_invalid_region(self):
        monitor = {"left": -1920, "top": 0, "width": 1920, "height": 1080}
        region = {"left": -1500, "top": 10, "width": 250, "height": 40}
        self.assertTrue(app.region_on_screen(region, [monitor]))
        self.assertFalse(app.region_on_screen(dict(region, left=-10), [monitor]))
        self.assertFalse(app.valid_region(dict(region, width=True)))
        self.assertFalse(app.valid_region(dict(region, height=0)))

    def test_config_roundtrip(self):
        with config_file() as path:
            config = dict(app.DEFAULTS, target="测试怪物")
            app.save_config(config, path)
            self.assertEqual(app.load_config(path), (config, ""))

    def test_bad_config_kept_and_defaults_restored(self):
        with config_file() as path:
            for data in ('{bad', '[]', '{"scale": 0}', '{"scale": NaN}',
                         '{"target": 1}', '{"region": {}}', '{"min_confidence": true}'):
                path.write_text(data, encoding="utf-8")
                config, warning = app.load_config(path)
                self.assertEqual(config, app.DEFAULTS)
                self.assertTrue(warning)
                self.assertEqual(path.read_text(encoding="utf-8"), data)

    def test_result_filtering_keeps_debug(self):
        box = [[0, 0], [20, 0], [20, 10], [0, 10]]
        names, debug = app.decode_results([(box, "腐皮食尸鬼", 0.9),
                                           (box, "野狼", 0.1)], 0.25)
        self.assertEqual(names, ["腐皮食尸鬼"])
        self.assertIn("野狼 (0.10)", debug)
        self.assertEqual(app.decode_results([], 0.25), ([], "无文本框"))


class WorkerTests(unittest.TestCase):
    def test_pause_discards_inflight_result_and_resume_recovers(self):
        entered, release = threading.Event(), threading.Event()
        box = [[0, 0], [30, 0], [30, 10], [0, 10]]
        calls = []

        def readtext(image, **kwargs):
            calls.append(image)
            if len(calls) == 1:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("测试超时")
            return [(box, "腐皮食尸鬼", 0.9)]

        class Capture:
            monitors = [{}, {"left": 0, "top": 0, "width": 1920, "height": 1080}]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def grab(self, region):
                return "frame"

        modules = {
            "easyocr": SimpleNamespace(Reader=lambda *a, **k: SimpleNamespace(readtext=readtext)),
            "torch": SimpleNamespace(set_num_threads=lambda n: None),
            "cv2": SimpleNamespace(cvtColor=lambda f, code: f, COLOR_BGRA2BGR=1),
            "mss": SimpleNamespace(MSS=Capture),
            "numpy": SimpleNamespace(array=lambda value: value),
        }
        with patch.dict("sys.modules", modules), patch.object(app, "prepare_image", lambda f, s: f):
            worker = app.OCRWorker()
            try:
                config = dict(app.DEFAULTS, region={"left": 1, "top": 1, "width": 100, "height": 20})
                worker.start(config)
                self.assertTrue(entered.wait(3))
                worker.pause()
                paused_generation = worker.generation
                release.set()
                # 恢复后只能发布新代号的结果，旧 OCR 不得触发目标提示。
                worker.start(config)
                while True:
                    generation, kind, payload = worker.events.get(timeout=3)
                    if kind == "result":
                        self.assertGreater(generation, paused_generation)
                        self.assertEqual(payload[0], ["腐皮食尸鬼"])
                        self.assertEqual(payload[2], 1)
                        break
                    self.assertNotIn(kind, ("error", "fatal"))
                self.assertGreaterEqual(len(calls), 2)
            finally:
                release.set()
                worker.close()


if __name__ == "__main__":
    unittest.main()
