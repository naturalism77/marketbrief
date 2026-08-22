"""
바탕화면 아이콘 클릭 시 실행 — 오늘(또는 최신) 시황 HTML을 브라우저로 엽니다.
"""
import os
import datetime
from pathlib import Path

import pytz

BASE_DIR   = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
KST        = pytz.timezone("Asia/Seoul")

today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
path  = OUTPUT_DIR / f"brief_{today}.html"

if not path.exists():
    htmls = sorted(OUTPUT_DIR.glob("brief_*.html"), reverse=True)
    path  = htmls[0] if htmls else None

if path:
    os.startfile(str(path))
else:
    import tkinter, tkinter.messagebox
    root = tkinter.Tk(); root.withdraw()
    tkinter.messagebox.showinfo("MarketBrief", "아직 생성된 시황 파일이 없습니다.\nmarketbrief.py를 먼저 실행하세요.")
