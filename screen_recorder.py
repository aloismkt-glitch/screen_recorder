"""
화면 녹화 프로그램 (Screen Recorder) v3
- Windows 전체 화면 + 시스템 오디오(WASAPI Loopback) 녹화
- 오디오: ffmpeg 파이프 직접 스트리밍 (메모리 누적 없음, 튀김 방지)
- 비디오: 15fps, 해상도 75%, CRF 26 (부하 경감 + 체감 화질 균형)
- 음성: 44.1kHz 16bit → AAC 192kbps (품질 유지)
- busy-wait 제거 → CPU 여유 확보

의존 라이브러리:
    pip install mss soundcard numpy imageio-ffmpeg keyboard
"""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import threading
import time
import os
import sys
import subprocess
import numpy as np
from datetime import datetime
import webbrowser

# ── 의존성 확인 ──────────────────────────────────────────────
_missing = []
try:
    import mss
except ImportError:
    _missing.append("mss")
try:
    import imageio_ffmpeg
except ImportError:
    _missing.append("imageio-ffmpeg")
try:
    import soundcard as sc
except ImportError:
    _missing.append("soundcard")
try:
    import keyboard
except ImportError:
    _missing.append("keyboard")

if _missing:
    print("다음 라이브러리를 설치하세요:")
    print(f"  pip install {' '.join(_missing)}")
    sys.exit(1)


def get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(sys.argv[0]))


# ── 녹화 엔진 ────────────────────────────────────────────────
class ScreenRecorder:
    """
    싱크 전략:
      - Barrier 동시 출발
      - 비디오: 누적 프레임 번호 기준 sleep (드리프트 보정, busy-wait 없음)
      - 오디오: ffmpeg 파이프로 실시간 스트리밍 (메모리 누적 없음)
      - mux 시 itsoffset + async 보정
    """

    VIDEO_FPS = 15
    VIDEO_SCALE = 0.75
    VIDEO_CRF = 26
    VIDEO_PRESET = "ultrafast"

    AUDIO_RATE = 44100
    AUDIO_CH = 2
    AUDIO_BITRATE = "192k"
    AUDIO_CHUNK_MS = 200  # 200ms 단위 (시스템 콜 절감)

    def __init__(self, output_path):
        self.output_path = output_path
        self.is_recording = False
        self.ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()

        self._barrier = threading.Barrier(2, timeout=10)
        self._video_start_ts = 0.0
        self._audio_start_ts = 0.0

        self._video_thread = None
        self._audio_thread = None
        self._audio_ok = True
        self._wall_start = None

    def start(self):
        self.is_recording = True
        self._wall_start = time.time()
        self._video_thread = threading.Thread(target=self._capture_video, daemon=True)
        self._audio_thread = threading.Thread(target=self._capture_audio, daemon=True)
        self._video_thread.start()
        self._audio_thread.start()

    def stop(self):
        self.is_recording = False
        if self._video_thread:
            self._video_thread.join(timeout=20)
        if self._audio_thread:
            self._audio_thread.join(timeout=20)
        self._mux()

    def get_elapsed(self):
        if self._wall_start:
            return time.time() - self._wall_start
        return 0.0

    # ── 비디오 캡처 ──────────────────────────────────────────
    def _capture_video(self):
        temp_video = self.output_path.replace(".mp4", "_v.mp4")
        log_path = self.output_path.replace(".mp4", "_ffmpeg_v.log")

        with mss.mss() as sct:
            mon = sct.monitors[1]
            raw_w, raw_h = mon["width"], mon["height"]

            out_w = int(raw_w * self.VIDEO_SCALE)
            out_h = int(raw_h * self.VIDEO_SCALE)
            out_w -= out_w % 2
            out_h -= out_h % 2

            cmd = [
                self.ffmpeg_path, "-y",
                "-f", "rawvideo",
                "-pix_fmt", "bgra",
                "-s", f"{raw_w}x{raw_h}",
                "-r", str(self.VIDEO_FPS),
                "-i", "pipe:0",
                "-vf", f"scale={out_w}:{out_h}",
                "-c:v", "libx264",
                "-preset", self.VIDEO_PRESET,
                "-crf", str(self.VIDEO_CRF),
                "-pix_fmt", "yuv420p",
                "-vsync", "cfr",
                temp_video,
            ]

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32" else 0,
            )

            interval = 1.0 / self.VIDEO_FPS
            frame_size = raw_w * raw_h * 4

            # Barrier 동시 출발
            try:
                self._barrier.wait()
            except threading.BrokenBarrierError:
                pass
            self._video_start_ts = time.perf_counter()

            frame_count = 0
            try:
                while self.is_recording:
                    img = sct.grab(mon)
                    raw = img.raw
                    if len(raw) != frame_size:
                        raw = raw[:frame_size].ljust(frame_size, b'\x00')
                    try:
                        proc.stdin.write(raw)
                    except (BrokenPipeError, OSError):
                        break
                    frame_count += 1

                    # 드리프트 보정 sleep (busy-wait 없음)
                    target = self._video_start_ts + frame_count * interval
                    sleep_dur = target - time.perf_counter()
                    if sleep_dur > 0:
                        time.sleep(sleep_dur)
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                proc.wait(timeout=30)
                log_file.close()
                try:
                    os.remove(log_path)
                except OSError:
                    pass

    # ── 오디오 캡처 → ffmpeg 파이프 직접 스트리밍 ─────────────
    def _capture_audio(self):
        temp_audio = self.output_path.replace(".mp4", "_a.wav")
        sr = self.AUDIO_RATE
        ch = self.AUDIO_CH
        chunk = int(sr * self.AUDIO_CHUNK_MS / 1000)

        try:
            speaker = sc.default_speaker()
            loopback = sc.get_microphone(
                id=str(speaker.name), include_loopback=True
            )
        except Exception as e:
            print(f"[오디오] 루프백 장치를 찾을 수 없습니다: {e}")
            self._audio_ok = False
            try:
                self._barrier.wait()
            except threading.BrokenBarrierError:
                pass
            self._audio_start_ts = time.perf_counter()
            self._write_silence(temp_audio, sr, ch)
            return

        # ffmpeg: stdin(raw PCM) → WAV 파일
        cmd = [
            self.ffmpeg_path, "-y",
            "-f", "s16le",
            "-ar", str(sr),
            "-ac", str(ch),
            "-i", "pipe:0",
            "-c:a", "pcm_s16le",
            temp_audio,
        ]

        log_path = self.output_path.replace(".mp4", "_ffmpeg_a.log")
        log_file = open(log_path, "w", encoding="utf-8")
        audio_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            creationflags=subprocess.CREATE_NO_WINDOW
            if sys.platform == "win32" else 0,
        )

        try:
            with loopback.recorder(samplerate=sr, channels=ch) as mic:
                # Barrier 동시 출발
                try:
                    self._barrier.wait()
                except threading.BrokenBarrierError:
                    pass
                self._audio_start_ts = time.perf_counter()

                while self.is_recording:
                    data = mic.record(numframes=chunk)
                    pcm = (data * 32767).clip(-32768, 32767).astype(np.int16)
                    try:
                        audio_proc.stdin.write(pcm.tobytes())
                    except (BrokenPipeError, OSError):
                        break
        except Exception as e:
            print(f"[오디오] 녹음 중 오류: {e}")
            self._audio_ok = False
        finally:
            try:
                audio_proc.stdin.close()
            except Exception:
                pass
            audio_proc.wait(timeout=15)
            log_file.close()
            try:
                if os.path.getsize(log_path) < 10000:
                    os.remove(log_path)
            except OSError:
                pass

    def _write_silence(self, path, sr, ch):
        import wave
        dur = max(self.get_elapsed(), 1.0)
        n = int(sr * dur)
        silence = np.zeros((n, ch), dtype=np.int16)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(ch)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(silence.tobytes())

    # ── 합성(mux) + 싱크 보정 ────────────────────────────────
    def _mux(self):
        temp_video = self.output_path.replace(".mp4", "_v.mp4")
        temp_audio = self.output_path.replace(".mp4", "_a.wav")

        if not os.path.exists(temp_video):
            print("[오류] 비디오 임시 파일이 없습니다.")
            return
        if not os.path.exists(temp_audio):
            print("[오류] 오디오 임시 파일이 없습니다.")
            return

        offset = self._audio_start_ts - self._video_start_ts
        abs_offset = abs(offset)

        cmd = [self.ffmpeg_path, "-y"]

        if abs_offset > 0.005:
            if offset > 0:
                cmd += ["-itsoffset", f"-{abs_offset:.4f}"]
                cmd += ["-i", temp_audio]
                cmd += ["-i", temp_video]
                audio_idx, video_idx = 0, 1
            else:
                cmd += ["-itsoffset", f"-{abs_offset:.4f}"]
                cmd += ["-i", temp_video]
                cmd += ["-i", temp_audio]
                audio_idx, video_idx = 1, 0
        else:
            cmd += ["-i", temp_video, "-i", temp_audio]
            audio_idx, video_idx = 1, 0

        cmd += [
            "-map", f"{video_idx}:v:0",
            "-map", f"{audio_idx}:a:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", self.AUDIO_BITRATE,
            "-async", "1",
            "-shortest",
            self.output_path,
        ]

        try:
            subprocess.run(
                cmd, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32" else 0,
            )
        except subprocess.CalledProcessError as e:
            print(f"[합성 오류] {e.stderr.decode(errors='replace')}")
            import shutil
            shutil.copy2(temp_video, self.output_path)

        for f in (temp_video, temp_audio):
            try:
                os.remove(f)
            except OSError:
                pass


# ── GUI ──────────────────────────────────────────────────────
class App:
    LINKS = [
        (
            "\u25b6 AI NEXT: 한눈에 읽는 AI 진화 계보",
            "https://play.google.com/store/books/details?id=2i7BEQAAQBAJ",
        ),
        (
            "\u25b6 약사가 알려주는 화장품 성분",
            "https://www.yes24.com/Product/Goods/179601623",
        ),
        (
            "\u25b6 을의 협상력: 중소기업 대표를 위한 거래 테이블 생존 전략",
            "https://play.google.com/store/books/details?id=K_e0EQAAQBAJ",
        ),
        (
            "\u25b6 고객의 속마음을 여는 대화법",
            "https://play.google.com/store/books/details?id=Vfe0EQAAQBAJ",
        ),
    ]

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("화면 녹화 프로그램")
        self.root.resizable(False, False)
        self.root.geometry("430x460")

        self.max_minutes = tk.IntVar(value=30)
        self.is_recording = False
        self.recorder = None
        self.current_segment = 1
        self.segment_start_time = None
        self.total_offset = 0.0
        self.file_base = None
        self.recording_window = None
        self.timer_label = None

        self._build_ui()

    def _build_ui(self):
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(
            label="최대 녹화 시간 직접 입력...", command=self._input_custom_time
        )
        menubar.add_cascade(label="파일", menu=file_menu)
        self.root.config(menu=menubar)

        main = ttk.Frame(self.root, padding=15)
        main.pack(fill="both", expand=True)

        setting_frame = ttk.LabelFrame(main, text="녹화 설정", padding=10)
        setting_frame.pack(fill="x", pady=(0, 8))

        ttk.Label(setting_frame, text="최대 녹화 시간 (분):").pack(anchor="w")

        radio_frame = ttk.Frame(setting_frame)
        radio_frame.pack(fill="x", pady=4)
        for m in (30, 60, 90, 120):
            ttk.Radiobutton(
                radio_frame, text=f"{m}분", variable=self.max_minutes, value=m
            ).pack(side="left", padx=4)

        ttk.Label(
            setting_frame,
            text="※ 장시간 녹화할 경우 프레임, 음성이 깨질 가능성이 높습니다.",
            foreground="red",
            wraplength=380,
        ).pack(anchor="w", pady=(4, 0))

        ttk.Button(
            main, text="\u25b6  전체 화면 녹화 시작", command=self._start_recording
        ).pack(fill="x", pady=10)

        ttk.Separator(main).pack(fill="x", pady=4)

        link_frame = ttk.LabelFrame(main, text="추천 도서", padding=8)
        link_frame.pack(fill="x", pady=4)

        for text, url in self.LINKS:
            btn = ttk.Button(
                link_frame, text=text, command=lambda u=url: webbrowser.open(u)
            )
            btn.pack(fill="x", pady=2)

        ttk.Separator(main).pack(fill="x", pady=4)

        ttk.Label(
            main,
            text="저장 경로: 실행파일과 같은 경로에 저장됩니다.",
            foreground="gray",
        ).pack(anchor="w")
        ttk.Label(
            main,
            text=(
                "면책 조항: 본 프로그램 사용으로 인해 발생하는 모든 문제에 대해 "
                "개발자는 일체 책임지지 않습니다."
            ),
            foreground="gray",
            wraplength=400,
        ).pack(anchor="w", pady=(2, 0))

    def _input_custom_time(self):
        val = simpledialog.askinteger(
            "최대 녹화 시간",
            "최대 녹화 시간(분)을 입력하세요:",
            initialvalue=self.max_minutes.get(),
            minvalue=1,
            maxvalue=360,
        )
        if val:
            self.max_minutes.set(val)

    def _start_recording(self):
        self.is_recording = True
        self.current_segment = 1
        self.total_offset = 0.0
        self.file_base = None

        output = self._make_path(1)
        self.recorder = ScreenRecorder(output)

        self.root.withdraw()
        self._open_recording_window()

        self.recorder.start()
        self.segment_start_time = time.time()

        try:
            keyboard.add_hotkey("ctrl+q", self._on_stop_hotkey)
        except Exception:
            pass

        self._tick()

    def _make_path(self, seg):
        base_dir = get_base_dir()
        if not self.file_base:
            self.file_base = f"녹화_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        name = self.file_base if seg == 1 else f"{self.file_base}_{seg}"
        return os.path.join(base_dir, f"{name}.mp4")

    def _open_recording_window(self):
        win = tk.Toplevel(self.root)
        win.title("녹화 중 - 00:00:00")
        win.geometry("420x90")
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self._stop_recording)
        self.recording_window = win

        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)

        self.timer_label = ttk.Label(
            frame,
            text="\u23fa 녹화 시간: 00:00:00",
            font=("맑은 고딕", 14, "bold"),
            foreground="red",
        )
        self.timer_label.pack(anchor="w")

        ttk.Label(
            frame,
            text=(
                "녹화를 종료하시려면 키보드 CTRL+Q를 누르시거나 "
                "우측 X버튼을 누르세요."
            ),
            wraplength=390,
        ).pack(anchor="w", pady=(4, 0))

        win.after(2000, win.iconify)

    def _tick(self):
        if not self.is_recording:
            return

        total = self.total_offset + self.recorder.get_elapsed()
        h, rem = divmod(int(total), 3600)
        m, s = divmod(rem, 60)
        ts = f"{h:02d}:{m:02d}:{s:02d}"

        try:
            if self.recording_window and self.recording_window.winfo_exists():
                self.timer_label.config(text=f"\u23fa 녹화 시간: {ts}")
                self.recording_window.title(f"녹화 중 - {ts}")
        except tk.TclError:
            pass

        seg_elapsed = time.time() - self.segment_start_time
        if seg_elapsed >= self.max_minutes.get() * 60:
            self._split()
        else:
            self.root.after(500, self._tick)

    def _split(self):
        self.total_offset += self.recorder.get_elapsed()
        self.recorder.stop()

        self.current_segment += 1
        output = self._make_path(self.current_segment)
        self.recorder = ScreenRecorder(output)
        self.recorder.start()
        self.segment_start_time = time.time()

        self.root.after(500, self._tick)

    def _on_stop_hotkey(self):
        self.root.after(0, self._stop_recording)

    def _stop_recording(self):
        if not self.is_recording:
            return
        self.is_recording = False

        try:
            keyboard.remove_hotkey("ctrl+q")
        except Exception:
            pass

        if self.recording_window and self.recording_window.winfo_exists():
            try:
                self.recording_window.deiconify()
                self.timer_label.config(text="저장 중... 잠시 기다려주세요.")
                self.recording_window.update()
            except tk.TclError:
                pass

        def _finalize():
            self.recorder.stop()
            self.root.after(0, self._on_finished)

        threading.Thread(target=_finalize, daemon=True).start()

    def _on_finished(self):
        if self.recording_window and self.recording_window.winfo_exists():
            self.recording_window.destroy()
        self.recording_window = None

        self.root.deiconify()
        messagebox.showinfo(
            "완료",
            f"녹화 파일이 저장되었습니다.\n저장 경로: {get_base_dir()}",
        )

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = App()
    app.run()
