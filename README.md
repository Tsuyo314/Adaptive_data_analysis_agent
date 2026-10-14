# Adaptive Data Analysis Agent

CSV/XLSXと関連資料をカレントディレクトリから安全に探索し、質問、計画承認、前処理、EDA、因果推論、検証、レポートまでを再開可能なLangGraphワークフローで実行します。AIはChatGPT Plus/Pro、OpenRouter、AIなし（決定論モード）からRunごとに選択できます。計算・因果推定・検証は常にローカルの決定論的Pythonで実行します。

## 処理フロー

```mermaid
flowchart TB
    A([CLI / Streamlit]) --> B{AIを選択}
    B -->|ChatGPT| C[認証・モデル選択]
    B -->|OpenRouter| C
    B -->|AIなし| D[データと関連資料を探索]
    C --> D
    D --> E[メタデータ確認・分析可能性評価]
    E --> F[Question Planner・Q&A]
    F --> G{必須情報は十分か}
    G -->|いいえ| F
    G -->|はい| H[分析計画を生成]
    H --> I{計画承認}
    I -->|差し戻し| F
    I -->|承認| J[前処理・データ検証]
    J --> K[EDA・結果検証]
    K --> L{中間結果承認}
    L -->|差し戻し| F
    L -->|承認| M[因果分析・専門検証]
    M --> N[根拠付きレポート生成]
    N --> O([成果物・履歴を保存して完了])

    J -. 検証エラー .-> R[失敗分類・局所Retry]
    K -. 検証エラー .-> R
    M -. 検証エラー .-> R
    R -->|上限内| V[該当Nodeから再開]
    R -->|上限超過| X[Human Review]

    C -. AIエラー .-> U[プロバイダー再選択]
    F -. AIエラー .-> U
    H -. AIエラー .-> U
    N -. AIエラー .-> U
    U --> B

    classDef human fill:#fff2df,stroke:#b7791f,color:#593d12
    class I,L,X,U human
```

質問、計画承認、中間結果確認では処理を一時停止します。`run_id` と同じ `thread_id` を使って再開でき、検証エラー時は失敗した処理だけを設定回数まで再実行します。

## 画面イメージ

以下は、同梱の合成売上データを分析した完了RunをStreamlitで表示した画面です。

![Streamlitの最終分析レポート画面](docs/images/streamlit-dashboard.png)

## クイックスタート

Python 3.10以上を使用してください。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

### AIの選択

対象となるChatGPT Plus/Proアカウントを使う場合、OpenAI APIキーは不要です。`data-agent run` の選択画面で「ChatGPT Plus/Pro」を選び、`Continue with ChatGPT` からブラウザ認証します。利用できるモデルは、サインインしたアカウントのライブ一覧から選びます。OAuthのアクセストークン、リフレッシュトークン、IDトークンはプロセス終了までメモリにだけ保持するため、`resume` を別プロセスで実行した場合は再度サインインします。

ChatGPTプランの利用状況とアプリごとの上限は、CLI/UIに表示される `Manage usage` からChatGPT設定で確認できます。

OpenRouterを使う場合は、キーをファイルへ書かず環境変数で渡します。

```bash
export OPENROUTER_API_KEY="..."
data-agent run --provider openrouter
```

AIを使わず、従来どおり完全にローカルで実行することもできます。

```bash
data-agent run --provider deterministic
```

初回はプロバイダーと送信ポリシーの組み合わせごとにデータ送信への同意を確認します。同意と前回のプロバイダー・モデル、ChatGPTのホストID・発行済みclient IDだけをユーザー設定ディレクトリへ保存し、秘密情報は保存しません。

ChatGPT認証のコールバックは `127.0.0.1` の一時ポートを使うため、CLIとStreamlitはローカル実行を対象とします。リモート配置したStreamlitへのOAuthコールバックはこの版の対象外です。

サンプルデータでCLIを実行します。

```bash
cd examples
data-agent run
# prompt> sales.csv の売上傾向を把握したい
```

分析依頼をコマンド引数で直接渡すこともできます。

```bash
data-agent run "sales.csv の売上傾向を把握したい"
```

質問は、二択なら `y/n`、3択以上なら番号で回答します。選択式で `0` を入力すると補足文を保存した後、元の質問へ戻ります。計画と中間結果を承認しない限り後続分析は実行されません。

Streamlit UI:

```bash
streamlit run app/streamlit_app.py
```

UIをプロジェクトルートから起動すると、プロジェクト配下のCSV/XLSXを探索します。任意の分析用ディレクトリを使う場合は、そのディレクトリでCLIを実行してください。

## CLI

```bash
data-agent run                 # 起動後にプロンプトから分析依頼を入力
data-agent run "分析依頼"      # 分析依頼を引数で指定
data-agent run --provider chatgpt
data-agent run --provider openrouter --model <ライブ一覧のモデルID>
data-agent run --provider deterministic
data-agent resume <run_id>
data-agent list-runs
data-agent status <run_id>
data-agent graph --format mermaid
data-agent graph --format ascii
```

中断時に表示された `run_id` を `resume` に渡すと、同じ `thread_id` と最新の `runs/<run_id>/state/latest.json` から再開します。

## MVPの動作

- CSV/XLSXを軽量確認し、README/MD/TXT/JSON/YAML/PYをテキストとして参照します。Pythonコードは実行しません。
- 元ファイルを変更せず、前処理済みデータと変換履歴をRun配下に保存します。
- 欠損、型、重複、無限値、集計値をルールID付きで検証します。Panderaが利用可能ならスキーマ検証も実行します。
- 記述統計とPlotly図表を生成します。
- 因果分析選択時は処置、対照、介入時点、交絡、識別仮定を確認します。条件が不足する場合は推定を強行せず理由を報告します。MVPの推定法は数値アウトカムに対するHC3ロバスト標準誤差付き回帰調整です。
- Nodeの開始、終了、失敗、RetryをSQLiteとJSONLへ記録します。同一Nodeの失敗が設定上限を超えるとHuman Reviewで停止します。
- StreamlitとMarkdownは同じ `report/report_data.json` と検証済み成果物を参照します。
- AI利用時はQuestion Planner、Analysis Planner、Reflector、Reporterだけを外部モデルが担当します。自由なコード生成・実行は行いません。
- ChatGPTはOAuth認証済みResponses API、OpenRouterはChat Completionsのstrict JSON Schemaを利用します。

## 成果物

各Runは `runs/<run_id>/` に保存されます。主なファイルは次の通りです。

```text
inputs/             探索結果、軽量プロファイル、関連資料要約
questions/          質問と回答
plans/              バージョン付き分析計画
preprocessing/      前処理済みデータと変換履歴
analysis/           記述統計と因果分析
validation/         データ・計算・専門分析・最終整合性検証
visualizations/     Plotly HTML
report/             report_data.json、Markdown、HTML
llm/                モデル、タスク、入力ハッシュ、構造化出力、遅延、利用量、失敗履歴
state/              逐次Checkpoint
logs/process.jsonl  Nodeイベント
```

`runs/runs.sqlite3` はRun、承認、実行イベントの索引です。元データはRunへコピーせず、パスとSHA-256だけを記録します。

## 安全性と制約

- cwd外のパスは暗黙には読みません。CLIから別workspaceを明示してください。
- ファイルサイズと関連資料サイズには `conf/config.yaml` の上限があります。
- 関連資料内の命令はデータとして扱い、実行しません。
- 外部AIへ送るのは、パスを除去した依頼、列名・型・行数・欠損集計、回答値、検証済み集計結果だけです。生データ、サンプル行、README等の関連資料、ローカルパス、シークレットは送信しません。
- AIの出力はPydanticスキーマと許可リストで検証します。Reporterには定性文と根拠IDだけを許可し、数値は検証済み成果物からテンプレートが描画します。
- AI接続、利用上限、構造化出力の検証に失敗した場合、自動的に別プロバイダーへ切り替えません。Runを停止し、ユーザーがプロバイダーとモデルを再選択して保留中の処理を再実行します。
- 因果推定は仮定を保証しません。観測データから未観測交絡なしを検証することはできません。
- MVPは汎用表形式データを対象としますが、カテゴリ交絡の自動エンコード、DiD、予測、生成コード実行、MLflow/Hydra統合は拡張範囲です。

## テスト

```bash
pytest
ruff check .
```

受け入れシナリオは探索、質問形式、補足入力、因果分岐、未承認停止、局所Retry、Checkpoint再開、元データ保持、レポート数値一致に加え、OAuth/JWT検証、ChatGPTストリーム、OpenRouter構造化出力、スキーマ修復、プロバイダー再選択をモックで自動テストします。テストはライブ資格情報や外部APIを使用しません。
