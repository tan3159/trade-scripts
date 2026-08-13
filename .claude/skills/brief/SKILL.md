---
name: brief
description: 前回セッションの状況（cache/brief.md）と 🙋 needs-human-input 滞留キューを表示する。ユーザーが「/brief」と言ったときや、セッション再開時に前回の状況を確認したいときに使う。
---

# /brief

前回セッションの状況を表示する。

## 手順

1. `cache/brief.md` を読んで内容をそのまま表示する。ファイルが存在しない場合は「briefがありません。一度セッションを終了すると自動生成されます。」と伝える。
2. 続けて、以下のコマンドを実行して 🙋 needs-human-input 滞留キューを表示する:

```bash
tidd human-queue --limit 5
```

## 出力形式

- まず「## 🙋 needs-human-input 滞留キュー」と見出しを表示する
- 件数を「滞留 N 件」と表示する（0 件の場合は「滞留なし」）
- 上位 5 件の Issue 番号・タイトル・経過日数を表示する
