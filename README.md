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

- Python 3.9+
- Google Chrome
- ChromeDriver（バージョンは Chrome に合わせること）

## セットアップ

```bash
# 依存パッケージをインストール
pip install -r requirements.txt

# ChromeDriver を配置
cp /path/to/chromedriver driver/chromedriver
chmod +x driver/chromedriver
```

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
