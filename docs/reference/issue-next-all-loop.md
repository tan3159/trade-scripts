# issue-next-all local runner

`issue-next-all` を任意の TiDD 対応リポジトリで繰り返し実行する runner です。
既定では現在ディレクトリを対象に、候補確認後の Codex 実行を最大5時間まで許可し、終了後5分待って次の Issue を確認します。
候補がなくなった場合は exit code 0 で終了します。

## 実行

`ai-dev-handbook` のルートから対象リポジトリを指定します。

```console
python scripts/issue-next-all-loop.py --repo /path/to/target-repository
```

対象リポジトリ自身に script を配置して実行する場合は、次だけで動きます。

```console
python scripts/issue-next-all-loop.py
```

`tidd_tools` の場所が標準と異なる場合は `--tidd-project PATH` で指定します。
実行間隔、1回の上限、クォータ回復待ち時間は、それぞれ `--interval`、`--max-run-seconds`、
`--quota-retry-seconds` で変更できます。

## クォータと再開

5時間上限、クォータ超過、rate limit を検出すると、runner は state を削除せずに待機して再試行します。
出力に `resets in 2h30m` のような回復時間があれば、その時間に1分を加えて待機します。
Ctrl-C / SIGTERM でも state は保持されるため、同じコマンドを再実行すると続きから再開できます。

## 注意

この runner は現行 Codex CLI の `--dangerously-bypass-approvals-and-sandbox` を使います。
対象リポジトリと実行環境を信頼できる場合だけ使用してください。
