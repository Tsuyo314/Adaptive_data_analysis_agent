# 分析プロジェクト

このフォルダの直下に、分析案件ごとのプロジェクトフォルダを作成します。

```text
data-analysis/
└── sales-project/
    ├── data/
    ├── references/
    └── .data-agent/runs/
```

次のコマンドでプロジェクトを作成できます。

```bash
data-agent init sales-project
data-agent run
```

プロジェクトフォルダと分析データはGit管理対象外です。
