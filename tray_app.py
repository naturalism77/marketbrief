"""
MarketBrief Tray App
────────────────────
시스템 트레이에 아이콘을 표시하고,
클릭하면 오늘 날짜 HTML 시황 브리핑을 브라우저로 엽니다.

의존성:
    pip install pystray pillow
"""

import os
import sys
import datetime
import webbrowser
import threading
import subprocess
from pathlib import Path

import pytz
import pystray
from PIL import Image, ImageDraw, ImageFont

# ── 경로 설정 ──────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
KST        = pytz.timezone("Asia/Seoul")

# ── 아이콘 생성 ───────────────────────────────────────────────
def make_icon(text: str = "MB", color: str = "#1a56db") -> Image.Image:
    """
    텍스트 + 배경색으로 트레이 아이콘 이미지 생성 (64×64 px).
    외부 .ico 파일 없이 동적으로 생성합니다.
    """
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 원형 배경
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    draw.ellipse([2, 2, size - 2, size - 2], fill=(r, g, b, 255))

    # 텍스트
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2, (size - th) / 2 - 2), text, fill="white", font=font)

    return img


def make_icon_alert() -> Image.Image:
    """빨간 아이콘 (오류 상태)"""
    return make_icon("!", "#e53935")


# ── 오늘 HTML 경로 ────────────────────────────────────────────
def today_html_path() -> Path | None:
    """오늘 날짜 HTML 파일 경로 반환. 없으면 None."""
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    path  = OUTPUT_DIR / f"brief_{today}.html"
    return path if path.exists() else None


def latest_html_path() -> Path | None:
    """output/ 폴더에서 가장 최신 HTML 파일 반환. 없으면 None."""
    htmls = sorted(OUTPUT_DIR.glob("brief_*.html"), reverse=True)
    return htmls[0] if htmls else None


# ── 브라우저 열기 ─────────────────────────────────────────────
def open_brief(icon, item=None):
    """오늘 HTML 우선, 없으면 최신 HTML 열기."""
    path = today_html_path() or latest_html_path()
    if path:
        os.startfile(str(path))
        icon.notify(f"브라우저에서 열었습니다:\n{path.name}", "MarketBrief")
    else:
        icon.notify("아직 생성된 시황 파일이 없습니다.", "MarketBrief")


# ── 시황 생성 (백그라운드) ────────────────────────────────────
def run_brief(icon, item=None):
    """
    marketbrief.py를 별도 프로세스로 실행합니다.
    완료되면 자동으로 브라우저를 엽니다.
    """
    icon.icon  = make_icon("⏳", "#fb8c00")
    icon.title = "MarketBrief — 생성 중..."

    def _run():
        try:
            script = str(BASE_DIR / "marketbrief.py")
            result = subprocess.run(
                [sys.executable, script, "--open"],
                capture_output=True, text=True, encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            if result.returncode == 0:
                icon.icon  = make_icon("MB")
                icon.title = "MarketBrief"
                icon.notify("시황 생성 완료!", "MarketBrief")
            else:
                icon.icon  = make_icon_alert()
                icon.title = "MarketBrief — 오류"
                icon.notify("시황 생성 실패. 로그를 확인하세요.", "MarketBrief")
        except Exception as e:
            icon.icon  = make_icon_alert()
            icon.title = "MarketBrief — 오류"
            icon.notify(f"오류: {e}", "MarketBrief")

    threading.Thread(target=_run, daemon=True).start()


# ── 아카이브(index.html) 열기 ─────────────────────────────────
def open_archive(icon, item=None):
    index = OUTPUT_DIR / "index.html"
    if index.exists():
        os.startfile(str(index))
    else:
        icon.notify("아카이브 파일이 없습니다.", "MarketBrief")


# ── 트레이 메뉴 ───────────────────────────────────────────────
def build_menu(icon) -> pystray.Menu:
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    has_today = today_html_path() is not None

    return pystray.Menu(
        pystray.MenuItem(
            f"📊 오늘 시황 보기 ({today})" + ("" if has_today else " [없음]"),
            open_brief,
            default=True,      # 더블클릭 시 실행
        ),
        pystray.MenuItem("📁 아카이브 (전체 목록)", open_archive),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🔄 지금 시황 생성", run_brief),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("❌ 종료", lambda icon, item: icon.stop()),
    )


# ── 메인 ─────────────────────────────────────────────────────
def main():
    icon = pystray.Icon(
        name  = "MarketBrief",
        icon  = make_icon("MB"),
        title = "MarketBrief",
        menu  = pystray.Menu(),   # 먼저 빈 메뉴로 생성
    )

    # 메뉴를 icon 참조 이후에 설정
    icon.menu = build_menu(icon)

    # Windows 알림이 작동하려면 setup callback 필요
    def setup(icon):
        icon.visible = True
        # 오늘 파일이 있으면 시작 시 자동으로 브라우저 오픈 (선택)
        # open_brief(icon)

    icon.run(setup)


if __name__ == "__main__":
    main()
