# STEP 1.5-e: 規模警告時の分割検討（Issue #3993）

`.claude/skills/issue-next/SKILL.md` STEP 1.5-d（PASS/FAIL いずれも）から遷移する。

issue-reviewer の JSON の `size_over_1000_possible` が **`false` の場合は本ステップを一切実行せず、
そのまま STEP 2 へ進む**（分割検討ステップは実行されない）。

**`true` の場合のみ**、STEP 2 へ直行せず以下を実行する。**`require-split-consideration.py` hook
が、分割検討の根拠コメント（`## 分割検討` マーカー）なしの `issue-implementer` 起動を exit code 2 で
機械ブロックする**（SKILL.md への動線記載だけでは強制力がないため）。

1. issue-reviewer の `size_reason`（分割案を含む・Issue #3993）を読み、実際に分割するかどうかを判断する
2. **分割する場合:** STEP 1.5-d「粒度が大きすぎる場合」と同じ手順で元 Issue を Epic 化し、
   `size_reason` の分割案を参考にサブ Issue を自動作成する。最初のサブ Issue を着手対象として STEP 2 へ進む
   （サブ Issue 側で改めて STEP 1.5 の品質チェックが走るため、本ステップの根拠コメントは不要）
3. **分割せず進む場合:** STEP 2 の `Agent(subagent_type="issue-implementer", ...)` 呼び出しより前に、
   想定 diff 行数の内訳（変更対象ファイル・見込み行数・テスト倍率考慮）を記載した根拠コメントを
   対象 Issue に投稿する（ヘッダ `## 分割検討` 必須）:
   ```bash
   gh issue comment <N> --body-file <一時ファイル>
   # 本文: "## 分割検討\n\n分割せず実装を進めます。\n\n### 想定 diff 行数の内訳\n\n- <ファイル/やること項目>: 約 <行数> 行\n- ...\n\n根拠: <issue-reviewer の size_reason の要約>"
   ```
   投稿後 STEP 2 へ進む
4. **`is-unattended <N>` が exit 0 のとき（#2802 の「人間へのエスカレーションで停止しない」原則に従う）:**
   人間の確認を待たず、オーケストレータが自律的に上記 2・3 のいずれかを判断して実行する。
   分割の可否は ai-dev-handbook 本体の docs/reference/ 配下・`pr-splitting-guide.md`（consumer 未配布）「分割してはいけないケース（例外リスト）」6 項目に
   該当するかどうかで判定し、いずれかに明確に該当する場合のみ 3（分割せず進む）を選び、
   該当しない・判定できない場合は 2（分割する）を優先する。いずれの分岐でも park はしない
