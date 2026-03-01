import tkinter as tk
from tkinter import filedialog
import cv2
import numpy as np
import mss
import time
import webbrowser


class ScreenRecorder:
    def __init__(self):
        self.init_window = tk.Tk()
        self.init_window.title('화면 녹화 준비')
        self.init_window.geometry('400x150')

        start_btn = tk.Button(self.init_window, text='녹화 영역 지정하기', command=self.start_selection, height=2)
        start_btn.pack(pady=20)

        copyright_label = tk.Label(
            self.init_window,
            text='Copyright ⓒ 2026 Alois Marketing All Rights Reserved.',
            fg='blue',
            cursor='hand2',
            font=('Arial', 10, 'underline')
        )
        copyright_label.pack(pady=10)
        copyright_label.bind('<Button-1>', lambda e: webbrowser.open('https://aloismkt.blogspot.com/p/english.html'))

        self.root = None
        self.start_x = None
        self.start_y = None
        self.rect = None
        self.region = None
        self.is_recording = False

    def start_selection(self):
        self.init_window.destroy()

        self.root = tk.Tk()
        self.root.attributes('-alpha', 0.3)
        self.root.attributes('-fullscreen', True)
        self.root.config(cursor='cross')

        self.canvas = tk.Canvas(self.root, cursor='cross', bg='grey11')
        self.canvas.pack(fill='both', expand=True)

        self.canvas.bind('<ButtonPress-1>', self.on_press)
        self.canvas.bind('<B1-Motion>', self.on_drag)
        self.canvas.bind('<ButtonRelease-1>', self.on_release)

    def on_press(self, event):
        self.start_x = event.x
        self.start_y = event.y
        self.rect = self.canvas.create_rectangle(self.start_x, self.start_y, 1, 1, outline='red', width=2)

    def on_drag(self, event):
        if self.start_x is None or self.start_y is None:
            return

        self.cur_x, self.cur_y = (event.x, event.y)
        self.canvas.coords(self.rect, self.start_x, self.start_y, self.cur_x, self.cur_y)

    def on_release(self, event):
        if self.start_x is None or self.start_y is None:
            return

        end_x, end_y = (event.x, event.y)
        self.region = {
            'top': min(self.start_y, end_y),
            'left': min(self.start_x, end_x),
            'width': abs(end_x - self.start_x),
            'height': abs(end_y - self.start_y)
        }
        self.root.destroy()
        self.start_recording()

    def start_recording(self):
        if not self.region or self.region['width'] == 0 or self.region['height'] == 0:
            print('유효하지 않은 영역입니다.')
            return

        save_root = tk.Tk()
        save_root.withdraw()
        file_path = filedialog.asksaveasfilename(
            defaultextension='.mp4',
            initialfile=f'record_{int(time.time())}.mp4',
            title='저장할 경로와 파일명을 지정하세요',
            filetypes=[('MP4 동영상', '*.mp4'), ('모든 파일', '*.*')]
        )
        save_root.destroy()

        if not file_path:
            print('저장 경로 선택을 취소했습니다. 프로그램을 종료합니다.')
            return

        self.is_recording = True
        print('녹화를 시작합니다. 중지하려면 소문자 q를 누르거나 창의 x 버튼을 누르세요.')
        self.record(file_path)

    def record(self, file_path):
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(file_path, fourcc, 20.0, (self.region['width'], self.region['height']))
        window_name = 'Recording... (Press q or close window to stop)'

        with mss.mss() as sct:
            while self.is_recording:
                img = np.array(sct.grab(self.region))
                frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
                out.write(frame)

                cv2.imshow(window_name, frame)

                if cv2.waitKey(1) & 0xFF == ord('q'):
                    self.is_recording = False
                    break

                if cv2.getWindowProperty(window_name, cv2.WND_PROP_VISIBLE) < 1:
                    self.is_recording = False
                    break

        out.release()
        cv2.destroyAllWindows()
        print(f'녹화가 완료되었습니다: {file_path}')


if __name__ == '__main__':
    app = ScreenRecorder()
    app.init_window.mainloop()