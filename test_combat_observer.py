"""红字停攻与简化后的 Q/E 集成测试；不发送真实游戏输入。"""
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import cv2

from auto_combat import CombatWorker, WindowsInput
from combat_observer import (ErrorMonitor, STOP_PHRASES, error_region, text_candidates,
                             template_score, stop_reason, ERROR_TEMPLATE, ocr_image)


class RedTextTests(unittest.TestCase):
    def test_three_supported_messages_and_ocr_spaces(self):
        for text in ("现在没有可以攻击的目标", "现在没有可攻击的目标", "目标已死亡",
                     "你不能攻击那个目标", "目 标 已 死 亡！", "你的目标已经死亡", "现荏没有可以攻击的目栋"):
            self.assertIsNotNone(stop_reason(text), text)

    def test_range_cooldown_stun_and_names_do_not_stop(self):
        for text in ("目标距离太远", "法术还没有准备好", "你正处于昏迷状态",
                     "现在没有足够的法力", "科卡尔队长", "目标血量20%", ""):
            self.assertIsNone(stop_reason(text), text)

    def test_default_region_tracks_client_window_and_negative_monitors(self):
        r = error_region(dict(left=-1920, top=100, width=1920, height=1080))
        self.assertEqual(r["left"], -1632)
        self.assertEqual(r["top"], 143)
        self.assertEqual(r["width"], 1344)
        self.assertEqual(r["height"], 432)

    def test_real_provided_template_and_resized_text(self):
        image = cv2.imdecode(np.frombuffer(ERROR_TEMPLATE.read_bytes(), np.uint8), 1)
        reference = text_candidates(image)[0]
        larger = cv2.resize(image, None, fx=1.5, fy=1.5)
        candidates = text_candidates(larger)
        self.assertGreater(template_score(reference, reference), 0.99)
        self.assertGreater(max(template_score(c, reference) for c in candidates), 0.88)

    def test_solid_health_bar_and_non_red_text_are_filtered(self):
        image = np.zeros((150, 500, 3), np.uint8)
        image[20:40, 20:400] = (0, 0, 220)
        cv2.putText(image, "TARGET DEAD", (30, 100), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 220, 0), 2)
        self.assertEqual(text_candidates(image), [])

    def test_multiple_red_lines_kept_for_ocr(self):
        a = np.zeros((20, 200), np.uint8)
        a[5:15, 20:180:4] = 255
        b = np.zeros((20, 100), np.uint8)
        b[3:16, 10:90:3] = 255
        image = ocr_image([a, b])
        self.assertEqual(image.shape, (72, 216))

    def monitor(self):
        m = ErrorMonitor.__new__(ErrorMonitor)
        m.native = SimpleNamespace(foreground=lambda: 123,
                                   client_bounds=lambda hwnd:dict(left=0, top=0, width=1920, height=1080))
        m.hwnd, m.cancel, m.events = 123, threading.Event(), queue.Queue()
        m.lock, m.frames = threading.Lock(), queue.Queue(maxsize=1)
        m.reason, m.error, m.ocr_thread = None, None, None
        return m

    def test_error_immediately_cancels_once(self):
        m = self.monitor()
        m.halt("目标已死亡")
        self.assertTrue(m.cancel.is_set())
        self.assertEqual(m.events.qsize(), 1)
        m.halt("现在没有可以攻击的目标")
        self.assertEqual(m.events.qsize(), 1)

    def test_stale_session_or_foreground_change_cannot_stop_new_work(self):
        m = self.monitor()
        m.native.foreground = lambda:456
        m.halt("目标已死亡")
        self.assertFalse(m.cancel.is_set())
        self.assertEqual(m.events.qsize(), 0)

    def test_template_detection_works_while_ocr_is_not_ready(self):
        m = self.monitor()
        image = cv2.imdecode(np.frombuffer(ERROR_TEMPLATE.read_bytes(), np.uint8), 1)
        image = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
        class Capture:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def grab(self,region): return image
        with patch("mss.MSS",Capture), patch.object(m,"ocr_loop",lambda:None):
            m.run()
        self.assertTrue(m.cancel.is_set())
        self.assertEqual(m.reason, STOP_PHRASES[0])


class WorkerIntegrationTests(unittest.TestCase):
    def scene(self, stop_on_error):
        now=[0.0]
        sent=[]
        used={"strike":-999., "judgment":-999.}
        monitors=[]
        events=queue.Queue()
        class Cancel:
            stopped=False
            def is_set(self): return self.stopped
            def set(self): self.stopped=True
            def wait(self,seconds):
                now[0]+=seconds
                if now[0]>=12: self.stopped=True
                return self.stopped
        worker=None
        def foreground():
            if stop_on_error and monitors and now[0]>=2:
                worker.cancel.set()
                events.put(("finished","目标已死亡"))
            return 123 if now[0]<12 else 456
        def send(key,hwnd,cancel):
            if cancel.is_set(): return False
            self.assertIn(key,("Q","E"))
            sent.append((now[0],key))
            if key=="E":
                # 模拟一个 E 只能先放出一个技能，需要紧跟第二次 E。
                for skill,cd in (("strike",10),("judgment",8)):
                    if now[0]-used[skill]>=cd:
                        used[skill]=now[0]
                        break
            return True
        native=SimpleNamespace(foreground=foreground,modifiers_down=lambda:False,send=send)
        class Capture:
            monitors=[{},dict(left=0,top=0,width=1920,height=1080)]
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def grab(self,region): return np.zeros((40,40,4),np.uint8)
        class Monitor:
            def __init__(self,*args):
                # 刻意没有 ready 属性；引擎不得等待 OCR 完成后才攻击。
                monitors.append(True)
        options=dict(stop_on_error=stop_on_error)
        for index,key in enumerate(("strike","judgment"),1):
            options[key+"_region"]=dict(left=index*100,top=50,width=40,height=40)
            options[key+"_reference"]=np.full((40,40,3),index,np.uint8)
        def state(frame,reference):
            skill="strike" if reference[0,0,0]==1 else "judgment"
            return now[0]-used[skill]>=(10 if skill=="strike" else 8),1.0
        worker=CombatWorker(native,events)
        worker.cancel=Cancel()
        with patch("auto_combat.time.monotonic",lambda:now[0]), patch("mss.MSS",Capture), \
                patch("auto_combat.ErrorMonitor",Monitor,create=True), \
                patch("auto_combat.classify_icon",return_value=(False,.7)), \
                patch("auto_combat.classify_attack_icon",state):
            worker.run(123,dict(left=0,top=0,width=40,height=40),np.zeros((40,40,3),np.uint8),.9,options)
        return sent,monitors,used

    def test_dual_ready_burst_then_no_e_during_cooldowns(self):
        sent,monitors,used=self.scene(False)
        self.assertEqual(monitors,[])
        self.assertGreaterEqual(len(sent),3)
        self.assertLess(sent[1][0]-sent[0][0],0.3)
        self.assertGreater(sent[2][0]-sent[1][0],7)
        self.assertTrue(all(key=="E" for _,key in sent))
        self.assertGreater(used["judgment"],0)

    def test_legacy_red_stop_option_is_ignored_and_combat_continues(self):
        sent,monitors,_=self.scene(True)
        self.assertEqual(monitors,[])
        self.assertGreaterEqual(len(sent),2)
        self.assertTrue(any(at>8 for at,_ in sent))

if __name__=="__main__":
    unittest.main()
