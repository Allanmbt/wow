"""技能、目标框和圣印舞的校验入口。"""

from ..combat.skills import ATTACK_SKILLS, TEMPLATE, HammerIcon, icon_image, validate_skill_regions
from ..vision.poison import PoisonDetector
from ..vision.seal import SealDetector, setup_complete
from ..vision.target import TargetFrameDetector, load_frame, saved_frame
from .selection import select_screen_region


class Calibration:
    def calibrate(self, key):
        if self.selecting:
            return
        self.stop()
        self.selecting = True
        self.log("框选愤怒之锤的完整图标，灰暗或点亮均可" if key == "hammer"
                 else "框选一个完整就绪图标 · 圣印需未开启 · 攻击技能需无倒计时")
        self.root.withdraw()
        def selected(region, frame):
            try:
                import cv2
                regions = dict(seal=self.region, **self.e_regions)
                regions[key] = region
                validate_skill_regions(regions, include_hammer=(key == "hammer"))
                if not (16 <= region["width"] <= 200 and 16 <= region["height"] <= 200
                        and 0.65 <= region["width"] / region["height"] <= 1.5 and frame.std() >= 8):
                    raise ValueError("请仅框选一个完整技能图标（16～200 像素）")
                if key != "seal":
                    center = cv2.cvtColor(icon_image(frame)[12:36, 12:36], cv2.COLOR_BGR2GRAY)
                    if center.std() < 3:
                        raise ValueError("图标中心缺少纹理，请重新框选")
                if key == "hammer":
                    state, _ = HammerIcon().classify(frame)
                    if state is None:
                        raise ValueError("未确认愤怒之锤图标，请在灰暗或点亮且无倒计时时框选")
                    if state is False:
                        self.e_regions[key] = region
                        self.persist()
                        self.update_labels()
                        return
                path = TEMPLATE if key == "seal" else ATTACK_SKILLS[key][2]
                ok, encoded = cv2.imencode(".png", frame)
                if not ok:
                    raise ValueError("PNG 编码失败")
                path.write_bytes(encoded.tobytes())
                if key == "seal":
                    self.region, self.reference = region, frame
                else:
                    self.e_regions[key], self.e_references[key] = region, frame
                self.persist()
                self.update_labels()
            except Exception as exc:
                self.status.set(f"定位失败：{exc}")
        self.root.after(200, lambda: select_screen_region(self.root, selected,
                           lambda: setattr(self, "selecting", False), include_frame=True))

    def calibrate_poison(self):
        if self.selecting:
            return
        self.stop()
        self.selecting = True
        self.monitor_status.set("请在中毒时框选自己的整排减益图标（含图标，不必包含倒计时）")
        self.root.withdraw()
        def selected(region, frame):
            try:
                if not (20 <= region["height"] <= 400 and 20 <= region["width"] <= 1200):
                    raise ValueError("请框选自己的减益栏，范围应在 1200×400 像素以内")
                hit = PoisonDetector().find(frame)
                if hit is None:
                    raise ValueError("未核验到这两种中毒图标，请在中毒时重新框选")
                old_region, old_size = self.poison_region, self.poison_icon_size
                self.poison_region, self.poison_icon_size = dict(region), hit.size
                try:
                    self.persist()
                except Exception:
                    self.poison_region, self.poison_icon_size = old_region, old_size
                    raise
                self.update_labels()
            except Exception as exc:
                self.monitor_status.set(f"净化校验失败：{exc}")

        def closed():
            self.selecting = False
            self.monitor_status.set("")

        self.root.after(200, lambda: select_screen_region(self.root, selected, closed, include_frame=True))

    def save_target_frame(self, profile):
        if profile == self.target_frame:
            return
        previous, self.target_frame = self.target_frame, profile
        try:
            self.persist()
        except OSError:
            self.target_frame = previous
            raise
        self.update_labels()

    def calibrate_target(self):
        if self.selecting:
            return
        bound = self.bound_window
        self.stop()
        self.selecting = True
        self.log("选中一个存活目标，框住目标框左侧完整的三行矩形区域")
        self.root.withdraw()

        def selected(region, image):
            try:
                match = TargetFrameDetector().locate(image)
                if not match:
                    raise ValueError("请选择名称栏红底的目标，再框住名称、血量和下方一行及左右边框")
                rect, reading = match
                rect = dict(rect, left=rect["left"] + region["left"], top=rect["top"] + region["top"])
                hwnd = self.native.window_at(rect["left"] + rect["width"] // 2, rect["top"] + rect["height"] // 2)
                if not self.native.is_game_window(hwnd):
                    hwnd = bound
                if not self.native.is_game_window(hwnd):
                    raise ValueError("框选区域需要位于游戏窗口内")
                profile = load_frame(saved_frame(rect, self.native.client_bounds(hwnd)))
                if profile is None:
                    raise ValueError("目标框超出游戏窗口")
                self.save_target_frame(profile)
                self.status.set(f"目标框已保存 · {reading.describe()}")
            except Exception as exc:
                self.status.set(f"目标框定位失败 · {exc}")

        self.root.after(200, lambda: select_screen_region(self.root, selected,
                        lambda: setattr(self, "selecting", False), include_frame=True))

    def calibrate_seal_dance(self, key):
        if self.selecting:
            return
        bound = self.bound_window
        self.stop()
        self.selecting = True
        self.root.withdraw()

        def selected(region, image):
            try:
                detector = SealDetector(self.seal_setup)
                setup = self.seal_setup.copy()
                if key == "righteous_icon":
                    setup[key], setup["storage_size"] = detector.learn_righteous(image)
                else:
                    if key == "current":
                        match = detector.locate_current(image)
                        if match is None:
                            raise ValueError("开启命令、正义或智慧圣印，框住完整金边图标和下方名称")
                        setup["current_format"], (left, top, width, height) = match
                        region = dict(left=region["left"] + left, top=region["top"] + top,
                                      width=width, height=height)
                    else:
                        stored, _, known = detector.read_storage(image)
                        if not stored or not known:
                            raise ValueError("有储存图标时，框住左侧这排储存增益的位置")
                    hwnd = self.native.window_at(region["left"] + region["width"] // 2,
                                                 region["top"] + region["height"] // 2)
                    if not self.native.is_game_window(hwnd):
                        hwnd = bound
                    if not self.native.is_game_window(hwnd):
                        raise ValueError("请在游戏窗口内框选")
                    profile = load_frame(saved_frame(region, self.native.client_bounds(hwnd)))
                    if profile is None:
                        raise ValueError("所选区域超出游戏窗口或太小")
                    setup[key] = profile
                previous, self.seal_setup = self.seal_setup, setup
                try:
                    self.persist()
                except OSError:
                    self.seal_setup = previous
                    raise
                self.update_labels()
                self.status.set("圣印校验已完成" if setup_complete(setup) else "已保存本项 · 可继续校验其他项")
            except Exception as exc:
                self.status.set(f"圣印校验失败 · {exc}")

        self.root.after(200, lambda: select_screen_region(self.root, selected,
                        lambda: setattr(self, "selecting", False), include_frame=True))
