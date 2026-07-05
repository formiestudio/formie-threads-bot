#!/usr/bin/env python3
"""
Threads自動投稿スクリプト

1. images/ フォルダから先頭(アルファベット順)の画像を1枚選ぶ
2. GitHub raw URL経由でClaude APIにキャプション生成を依頼
3. Threads Graph API v1.0でメディアコンテナ作成 → 30秒待機 → publish
4. 成功したら画像を posted/ に移動し、git commit & push する
5. images/ が空の場合はエラーにせず警告ログのみ出して終了する
"""

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import anthropic
import requests

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger("post")

REPO_ROOT = Path(__file__).resolve().parent
IMAGES_DIR = REPO_ROOT / "images"
POSTED_DIR = REPO_ROOT / "posted"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

CLAUDE_MODEL = "claude-sonnet-4-6"

CAPTION_PROMPT = """あなたはFormie Studio(オーダーメイド家具・壁面収納をつくる会社)のSNS担当者です。
この画像を見て、Threadsに投稿するキャプションを日本語で作成してください。

条件:
- Formie Studioらしい、温かみのある丁寧な文体
- 200文字以内(ハッシュタグ含む)
- 最後にハッシュタグを5〜8個つける(例: #formie #オーダー家具 #壁面収納 #おうち時間 #収納アイデア など、画像内容に合わせて調整してよい)
- キャプション本文以外の説明や前置きは一切書かず、投稿するテキストのみを出力すること
"""


def get_env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "")
    if required and not value:
        logger.error(f"環境変数 {name} が設定されていません。")
        sys.exit(1)
    return value


def pick_next_image() -> Path | None:
    if not IMAGES_DIR.exists():
        logger.warning(f"{IMAGES_DIR} が存在しません。")
        return None

    candidates = sorted(
        p for p in IMAGES_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )

    if not candidates:
        logger.warning("images/ に投稿対象の画像がありません。今回はスキップします。")
        return None

    return candidates[0]


def build_raw_url(owner: str, repo: str, filename: str) -> str:
    return f"https://raw.githubusercontent.com/{owner}/{repo}/main/images/{filename}"


def generate_caption(image_url: str, api_key: str) -> str:
    logger.info("Claude APIでキャプションを生成します。")
    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=500,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "url", "url": image_url},
                    },
                    {"type": "text", "text": CAPTION_PROMPT},
                ],
            }
        ],
    )

    caption = "".join(
        block.text for block in message.content if block.type == "text"
    ).strip()

    if not caption:
        raise RuntimeError("Claude APIから空のキャプションが返されました。")

    logger.info(f"生成されたキャプション:\n{caption}")
    return caption


def create_media_container(user_id: str, access_token: str, image_url: str, caption: str) -> str:
    logger.info("Threadsメディアコンテナを作成します。")
    resp = requests.post(
        f"https://graph.threads.net/v1.0/{user_id}/threads",
        params={
            "media_type": "IMAGE",
            "image_url": image_url,
            "text": caption,
            "access_token": access_token,
        },
        timeout=60,
    )

    if not resp.ok:
        logger.error(f"コンテナ作成に失敗しました: {resp.status_code} {resp.text}")
        resp.raise_for_status()

    container_id = resp.json().get("id")
    if not container_id:
        raise RuntimeError(f"コンテナIDが取得できませんでした: {resp.text}")

    logger.info(f"コンテナ作成成功: id={container_id}")
    return container_id


def publish_container(user_id: str, access_token: str, container_id: str) -> None:
    logger.info("Threadsに投稿(publish)します。")
    resp = requests.post(
        f"https://graph.threads.net/v1.0/{user_id}/threads_publish",
        params={
            "creation_id": container_id,
            "access_token": access_token,
        },
        timeout=60,
    )

    if not resp.ok:
        logger.error(f"publishに失敗しました: {resp.status_code} {resp.text}")
        resp.raise_for_status()

    logger.info(f"投稿成功: {resp.json()}")


def run(cmd: list[str]) -> None:
    logger.info(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def move_and_push(image_path: Path, owner: str, repo: str, gh_token: str) -> None:
    POSTED_DIR.mkdir(exist_ok=True)
    dest = POSTED_DIR / image_path.name
    image_path.rename(dest)
    logger.info(f"{image_path.relative_to(REPO_ROOT)} を posted/ に移動しました。")

    run(["git", "config", "user.name", "github-actions[bot]"])
    run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"])
    run(["git", "add", "images", "posted"])

    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=REPO_ROOT,
    )
    if result.returncode == 0:
        logger.info("git差分がないためcommitをスキップします。")
        return

    run(["git", "commit", "-m", f"chore: move {image_path.name} to posted/"])

    remote_url = f"https://x-access-token:{gh_token}@github.com/{owner}/{repo}.git"
    run(["git", "push", remote_url, "HEAD:main"])


def main() -> None:
    anthropic_api_key = get_env("ANTHROPIC_API_KEY")
    threads_user_id = get_env("THREADS_USER_ID")
    threads_access_token = get_env("THREADS_ACCESS_TOKEN")
    gh_token = get_env("GH_TOKEN")
    owner = get_env("REPO_OWNER")
    repo = get_env("REPO_NAME")

    image_path = pick_next_image()
    if image_path is None:
        return

    image_url = build_raw_url(owner, repo, image_path.name)
    logger.info(f"対象画像: {image_path.name} ({image_url})")

    caption = generate_caption(image_url, anthropic_api_key)
    container_id = create_media_container(threads_user_id, threads_access_token, image_url, caption)

    logger.info("publishまで30秒待機します。")
    time.sleep(30)

    publish_container(threads_user_id, threads_access_token, container_id)
    move_and_push(image_path, owner, repo, gh_token)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("処理中にエラーが発生しました。")
        sys.exit(1)
