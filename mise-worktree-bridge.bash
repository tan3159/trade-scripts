# mise-worktree-bridge.bash — worktree 用 mise 環境変数ブリッジ（Issue #3869）
#
# `tidd worktree-add` は config.json の `worktree-mise-stub-path` が設定されている場合、
# 新規 worktree ルートに `.mise.toml` スタブ（`[env]\n_.source = "<このファイルへのパス>"`）を
# 自動生成する（#3618）。git worktree は本体リポジトリの兄弟ディレクトリに作られるため、
# mise の `.mise.toml` 親方向探索では本体リポジトリの設定に届かない。本ファイルはその
# 橋渡し役として、`worktree-mise-stub-path` の参照先に指定して使う。
#
# やること: 自身の配置場所（このファイル自身のパス）から本体リポジトリのルートを動的に
# 解決し、`eval "$(mise env -C "<本体リポジトリルート>")"` を実行するだけ。
#
# 秘密情報の実値は一切含まない（参照先を動的に解決するロジックのみ）。
# 環境固有パス（`$HOME/repos/...` 等）をハードコードしない — consumer リポジトリが
# どの絶対パスにクローンされていても、そのままの内容で動作する。
#
# 配置前提: 本ファイルは本体リポジトリのルート直下（`.mise.toml` と同じ階層）に置き、
# config.json の `worktree-mise-stub-path` にこのファイルの絶対パスを設定する
# （手順の詳細: docs/setup/secrets-management.md「worktree での mise スタブ自動生成」）。
# リポジトリ外（`/tmp` 等）へ単体でコピーすると、自身の配置場所からリポジトリルートを
# 解決できないため意図的にエラーで停止する。
#
# Windows Git Bash 注意（#3899）: mise は Windows 上では `PATH` を Windows 形式
# （`;` 区切り・`C:\...`）で出力する。Git Bash は `:` 区切りの POSIX 形式（`/c/...`）
# しか解釈できないため、`PATH` 行をそのまま eval すると PATH が破壊され `git`・`python3`
# 等のコマンドが解決できなくなる。そのため `PATH` 行のみ除外して eval する
# （worktree シェルの PATH は Git Bash が起動時に設定した値をそのまま引き継ぐ）。

_mise_worktree_bridge_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

if _mise_worktree_bridge_repo_root="$(git -C "$_mise_worktree_bridge_dir" rev-parse --show-toplevel 2>&1)"; then
    eval "$(mise env -C "$_mise_worktree_bridge_repo_root" | grep -v '^export PATH=')"
else
    echo "mise-worktree-bridge: ${_mise_worktree_bridge_dir} はリポジトリ内ではありません（${_mise_worktree_bridge_repo_root}）" >&2
    echo "mise-worktree-bridge: 本ファイルは本体リポジトリのルート直下に配置してください（詳細: docs/setup/secrets-management.md）" >&2
    return 1 2>/dev/null || exit 1
fi

unset _mise_worktree_bridge_dir _mise_worktree_bridge_repo_root
