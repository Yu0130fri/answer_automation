# scrape_moppy

Moppy（モッピー）のアンケートを自動で回答するツールです。Selenium + ChromeDriver を使ってヘッドレスブラウザで動作します。

## 機能

- アンケート一覧の自動取得・フィルタリング
- ラジオボタン（通常形式・テーブル形式）、チェックボックス、セレクトボックス、テキスト入力への自動回答
- Cookie によるセッション管理（再ログイン不要）
- 回答済み URL / 回答不可 URL の記録（重複スキップ）
- psutil によるリソース監視とドライバ自動再起動
- 排他・混雑キーワードの検出によるスキップ

## 必要環境

- Python 3.14.8 (Python 3.14 stable maintenance release; Python does not designate LTS releases)
- Google Chrome
- ChromeDriver（バージョンは Chrome に合わせること）

## セットアップ

```bash
# macOS (Homebrew): Python 3.14 の最新版を導入・更新
brew update
brew install python@3.14
# すでにインストール済みの場合は brew upgrade python@3.14

# Python 3.14専用の仮想環境を作成
"$(brew --prefix python@3.14)/bin/python3.14" -m venv .venv
source .venv/bin/activate

# 依存パッケージをインストール
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 実行中のPythonバージョンを確認
python --version
# Python 3.14.8

# ChromeDriver を配置
cp /path/to/chromedriver driver/chromedriver
chmod +x driver/chromedriver
```

Pythonには公式のLTS指定はありません。Python 3.14系は安定版としてメンテナンスされており、現在のサポート予定は2030年10月までです。3.15系が安定版になるまでは、3.14系の最新メンテナンスリリースを使用してください。既存のPython/pyenv環境は削除・上書きせず、このプロジェクトの`.venv`内で切り替えます。

## 使い方

### 初回：Cookie の保存

```bash
python -c "
from selenium_moppy import AnswerQuestionnaire
aq = AnswerQuestionnaire(email='your@email.com', password='yourpassword')
aq.save_cookie_as_pickle()
"
```

### アンケート自動回答

```bash
python main.py --email your@email.com --password yourpassword
```

### 並列実行（3 worker）

```bash
python main.py --email your@email.com --password yourpassword --threads 3
```

`--threads` を指定すると、同じMoppyアカウントで Chrome を並列起動し、アンケートURLを分割して同時に回答します。各 worker のログは `worker-1`, `worker-2`, `worker-3` の形式でまとめて表示されます。

### 環境変数 (.env) からの読み込み

`--email`/`--password` をコマンドラインで渡す代わりにルートに `.env` ファイルを置くことで自動読み込みできます（例）:

```
EMAIL=your@email.com
PASSWORD=yourpassword
```

`.env` はリポジトリの .gitignore に含めることを強く推奨します。絶対に公開リポジトリにコミットしないでください。コードは .env を平文で読み込むだけなので、取り扱いには細心の注意を払ってください。

この実装では `python-dotenv` が優先的に使われます。もし未インストールの場合は内蔵の簡易ローダで読み込みを行います。推奨: `pip install -r requirements.txt` を実行して `python-dotenv` をインストールしてください。

`.env` のテンプレートは `.env.sample` に用意しています。これをコピーして `.env` を作成してください。
### 途中停止 / 再開

```bash
python main.py --threads 3
```

実行中に `Ctrl+C` で停止しても、進行中・完了済みの URL 状態は `.moppy_parallel_state.json` に保存されます。次回同じコマンドを再実行すると、自動で未処理分だけ再開します。

初期状態からやり直したい場合は:

```bash
python main.py --reset-state
```

## ファイル構成

```
scrape_moppy/
├── main.py                          # エントリーポイント
├── selenium_moppy/
│   └── answer_automation.py         # 自動回答ロジック
├── driver/
│   └── chromedriver                 # ChromeDriver（要配置）
├── moppy.pkl                        # 保存された Cookie（.gitignore 対象）
├── unable_to_answer.csv             # 回答不可 URL リスト（.gitignore 対象）
├── answered.csv                     # 回答済み URL リスト（.gitignore 対象）
└── requirements.txt
```

## 注意事項

- `moppy.pkl` には認証情報が含まれるため、リポジトリにはコミットしないでください。
- 実行は自己責任で行ってください。サイトの利用規約を確認の上ご使用ください。
