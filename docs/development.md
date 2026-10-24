# 開発環境

## 必要なもの

- Python 3.12以降
- Git
- ローカルWeb画面を使う場合はbrowser
- Node.jsは任意。JavaScript構文をローカル確認するときだけ必要

repository rootからpackage、test、lint用toolを導入します。

~~~shell
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
~~~

Windows PowerShellの場合:

~~~powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
$env:SHELTERSIM_LLM_MODE = "offline"
~~~

任意の施設図面画像処理は python -m pip install -e ".[dev,plan-import]"、Claude連携は python -m pip install -e ".[dev,llm]" で追加します。

## 設定

packageは環境変数から設定を読み込み、.envを自動読込しません。.env.exampleは変数名と安全な例の一覧です。実keyはshellのsecret managerまたはローカル環境に置き、commitしないでください。

testではoffline modeを明示します。

~~~shell
export SHELTERSIM_LLM_MODE=offline
~~~

意図してAPIを使う場合だけANTHROPIC_API_KEYとSHELTERSIM_LLM_MODE=autoを設定します。routerは設定されたrequest数、同時実行数、token数、費用上限を適用します。live Providerの前にllm-integration.mdを確認してください。

## よく使うcommand

~~~shell
sheltersim --help
sheltersim model-catalog --scenario fixtures/scenarios/h00_small_replay.json
sheltersim prepare --scenario fixtures/scenarios/h00_small_replay.json --freeze --output /tmp/approved.json
sheltersim run --offline --scenario /tmp/approved.json --output /tmp/sheltersim-run
sheltersim inspect --run /tmp/sheltersim-run --events
sheltersim compare --help
sheltersim project --help
~~~

Project CLIでは、Project作成、確認済みScenario保存、Run作成、Branch再開・分岐、履歴検索、利用記録確認ができます。Project dataは既定でruns/.projectsに保存します。CLIの--rootで別のローカルdirectoryを指定できます。

## ローカルWebアプリ

offline workspaceとInspectorの起動方法はdemo-guide.mdを参照してください。serverは既定で127.0.0.1にbindします。dataは設定されたRun/Project rootの下にローカル保存します。認証がないため、hostedな複数利用者向けサービスではありません。

## packageと任意依存

pyproject.tomlでcoreと開発依存を固定しています。Anthropic SDKと施設図面画像/PDF helperは任意依存です。PDF.jsはsrc/sheltersim/web/static/vendor/pdfjsに同梱され、licenseとnoticeを保持しています。
