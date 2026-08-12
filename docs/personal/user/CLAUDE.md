# CLAUDE.md（Claude Code ブリッジ）

常時適用ルールの正（canonical）は repo root の `AGENTS.md`。ただし `AGENTS.md` は
Codex CLI 向けに「コア部」+ `.claude/rules/*.md` の本文を rulesync でインライン展開した
生成物で、Claude Code はその `.claude/rules/*.md` を別経路で自動ロードする。そのため
`AGENTS.md` を丸ごと import すると同一ルール本文が毎セッション二重にロードされる（#3435）。
下記の import 1 行では、両者と重複しない「コア部」の rulesync 正本
`.rulesync/rules/overview.md` のみを取り込む。

@../../../.rulesync/rules/overview.md

<!-- このファイルは上流テンプレートが配布する共通ブリッジです。
consumer 独自の記述はここに書かないでください。
独自の追記が必要な場合は repo-specific マーカー
（AGENTS.md の `<!-- BEGIN: repo-specific -->` 〜 `<!-- END: repo-specific -->` 内）に
書いてください。このマーカー内は `copier update` で上書きされません。 -->
