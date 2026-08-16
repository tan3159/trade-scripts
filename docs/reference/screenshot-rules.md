# スクリーンショット参照ルール

Claude Code・Codex がスクリーンショットを参照・添付するときの手順。

**対応 OS: WSL / Windows ネイティブのみ。** macOS・Linux ネイティブ環境は自動推定非対応。
それらの環境では `--screenshot-dir` オプションまたは `SCREENSHOT_DIR` 環境変数を明示指定してください。

## Claude Code / Codex 対応表

`tidd screenshot-attach --latest` が出力した画像パスは、実行中の AI エージェントに応じて
異なるツールで開く。

| AI エージェント | 画像を開くツール |
|-----------------|------------------|
| Claude Code | `Read` |
| Codex | `view_image` |

## 最新のスクリーンショットを参照する

「最新のスクショを見て」「スクリーンショットを確認して」などと指示された場合:

1. `tidd screenshot-attach --latest` を実行してパスを取得する
2. 取得したパスのファイルを、Claude Code は `Read`、Codex は `view_image` で参照する（対応表参照）

```bash
tidd screenshot-attach --latest
# stdout に最新スクリーンショットのパスが出力される
# 例: /mnt/c/Users/<ユーザー名>/Pictures/Screenshots/スクリーンショット 2025-06-01 120000.png
```

出力されたパスを Claude Code は `Read`、Codex は `view_image` に渡して画像を参照する。

## スクリーンショットを MD に添付する

「スクショを MD に添付して」「スクリーンショットをドキュメントに貼って」などと指示された場合:

```bash
tidd screenshot-attach --attach <MD_FILE>
# 最新スクリーンショットを webp に変換して <MD_FILE> と同じディレクトリに配置し、
# <MD_FILE> に スクリーンショット画像（webp 変換後）への参照を追記する
```

特定のスクリーンショットを指定したい場合:

```bash
tidd screenshot-attach --attach <MD_FILE> --screenshot <IMG_FILE>
```

## webp 変換が失敗する場合

スクリプトは webp 変換に ImageMagick（`magick` または `convert`）か `ffmpeg` を使用する。
見つからない場合は `WARN: convert/magick/ffmpeg が見つかりません` と表示されて元ファイルをコピーするだけになる（拡張子は `.webp` だが中身は元形式のまま）。

Claude Code は非インタラクティブシェルで動くため `mise activate bash`（`.bashrc` 記載）が効かず、
mise のシムが PATH に入らないことがある。スクリプトは `~/.local/share/mise/shims/magick` を直接確認するため、
mise で ImageMagick がインストール済みであれば警告が出ても自動的に変換される。

ImageMagick が未インストールの場合のみ以下を実行する:

```bash
mise install imagemagick
mise use imagemagick
```

また ImageMagick v7 では `convert` コマンドは廃止され `magick` に統合された。スクリプトは両方を確認する。

## SCREENSHOT_DIR が未設定の場合

`SCREENSHOT_DIR` 環境変数が設定されていない場合、スクリプトが OS 別にディレクトリを自動推定する。

| 環境 | 自動推定パス |
|------|------------|
| WSL (Linux + microsoft カーネル) | `/mnt/c/Users/<ユーザー名>/Pictures/Screenshots` |
| Windows ネイティブ | `%USERPROFILE%\Pictures\Screenshots` |
| macOS・Linux ネイティブ | **自動推定なし（エラー）** |

macOS・Linux ネイティブで自動推定エラーが出た場合は以下を確認する:

```
ERROR: screenshot-attach は WSL / Windows ネイティブのみ対応です。
```

このエラーが出た場合は `--screenshot-dir` オプションで明示指定する:

```bash
tidd screenshot-attach --screenshot-dir /path/to/screenshots --latest
```

自動推定に失敗した場合（WSL / Windows で推定パスが存在しない場合）は以下のいずれかで設定するよう案内する:

- `export SCREENSHOT_DIR=/mnt/c/Users/<ユーザー名>/Pictures/Screenshots`
- `~/.bashrc` に上記の行を追記する
- `--screenshot-dir` オプションで一時的に指定する

## 参照

- コマンド: `tidd screenshot-attach`（`tidd_tools` に同梱）
- 環境変数: `SCREENSHOT_DIR`
