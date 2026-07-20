# 決定の要約

上流 ai-dev-handbook の反映（Issue #24）は安定タグ経由の copier update で行う。ただし新タグは上流 #2272（CircleCI nightly-tests failure）の修正マージ後に切る。

- **決定日:** 2026-07-19
- **記録者:** Claude（ユーザー回答に基づく）
- **参照:** tan3159/trade-scripts#24, tan3159/trade-scripts#18, being-gaia-plan/ai-dev-handbook#2272

## 論点

1. 上流 main はタグ `v2026.07.17.3` から 23 コミット先行しているが次期タグ未リリースで #24 が blocked。どの経路で反映するか。
2. タグを切ろうとしたところ、上流 main HEAD の commit status に `nightly-tests` failure（既知 Issue #2272・CircleCI 環境の copier git identity 問題・別セッションで修正中）。手順書の前提「main が安定した状態で実行」を満たさない中でタグを切るか。

## 提示した選択肢

### 論点 1（反映経路）

| 案 | 内容 | トレードオフ |
|---|---|---|
| A（推奨） | 上流で次期タグを切ってから copier update | 安定タグ運用を維持しつつ即反映。上流へのタグ作成が必要 |
| B | `--vcs-ref=HEAD` で main に直接 update | 即反映だが pin がコミットハッシュになり安定タグ運用から逸脱 |
| C | 上流の通常タグリリース待ち | 安全だが反映されない |

### 論点 2（タグのタイミング）

| 案 | 内容 | トレードオフ |
|---|---|---|
| A（推奨） | 既知の環境起因 failure として今すぐタグ | 早いが「main 安定」前提を厳密には満たさない |
| B | #2272 修正マージを待ってからタグ | 前提を厳密に満たすが反映が遅れる |

## ユーザーの決定

- 論点 1: **A**（タグを切ってから copier update）
- 論点 2: **B**（#2272 マージを待つ）

## 理由・背景

安定タグ運用は維持する。タグは「main が安定した状態」で切るという上流手順書（copier-template-maintenance.md）の前提を厳密に守り、CI failure が残る状態ではタグを切らない。

## 今後 AI が取るべき行動

1. 上流 #2272 のマージ（＝main の nightly-tests GREEN 化）を確認したら、上流で `v<YYYY.MM.DD>` 日付タグを切る（`git tag -a` + push、s-tanaka-being アカウントで admin 権限あり）
2. その後 #24 に着手: 新規 worktree（origin/main 起点なので untracked 3 ファイルの dirty 問題なし）で `tidd copier-update` → uv tool の tidd を同タグへ `--force` 再インストール → hook 登録・skill パス修正・hooks-enablement.md 更新を検証
3. #18 の .gitignore ブランチ（build/issue-18-untracked-files、PR 未作成）は新タグ取り込みで不要になる見込み。#24 完了時に整理を判断
4. `--vcs-ref=HEAD` での反映提案は今後行わない
