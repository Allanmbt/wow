"""Regression coverage for the shared E skills and legacy settings."""

import queue
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from wow_assistant.combat.worker import CombatWorker


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
        with patch("wow_assistant.combat.worker.time.monotonic",lambda:now[0]), patch("mss.MSS",Capture), \
                patch("wow_assistant.combat.worker.ErrorMonitor",Monitor,create=True), \
                patch("wow_assistant.combat.worker.classify_icon",return_value=(False,.7)), \
                patch("wow_assistant.combat.worker.classify_attack_icon",state):
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
