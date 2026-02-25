⚠️ このREADMEは生成AIによって作成しています。間違いがあったらすみません🙇‍♂️

# CoT Prompting - Automated Bug Fix via Few-shot CoT

GitHubのコミット履歴から REST API 誤用の修正例を収集・抽出し、Few-shot CoT（Chain-of-Thought）プロンプトを自動構築することで、LLM によるソースコードの自動バグ修正を実現するツールです。

## 概要

```
GitHub コミット履歴
       ↓ (get-code.py)
before/after ペア収集
       ↓ (0_filtering.py)
REST API 誤用修正ペアの抽出
       ↓ (1_dataset.py)
OpenAI API による誤用修正判定 → 修正例データセット
       ↓ 【手動】1_dataset.json を目視確認し、適切な事例に isEnabled: true を記入
       ↓ (2_context.py)
カテゴリ分類 + CoT メッセージ付き修正例の生成
       ↓ (3_prompt.py)
Few-shot CoT プロンプト構築 → LLM によるソースコード自動修正
```

各ステップの詳細は「[detectーcode データ生成パイプライン](#detectーcode-データ生成パイプライン)」を参照してください。

## セットアップ

### 1. GitHub Personal Access Tokenの取得

1. GitHub Settings → Developer settings → Personal access tokens → Tokens (classic)
2. "Generate new token (classic)" をクリック
3. 以下のスコープを選択：
   - `repo` (リポジトリへのフルアクセス)
   - `public_repo` (公開リポジトリのみの場合)
4. トークンを生成してコピー

### 2. 環境変数の設定

プロジェクトのルートディレクトリに `.env` ファイルを作成：

```bash
GITHUB_TOKEN=your_github_personal_access_token_here
```

⚠️ `.env` ファイルは `.gitignore` に含まれており、Gitリポジトリには含まれません。
`cp .env.example .env` でテンプレートをコピーしてから値を設定すると安全です。

### 3. 依存関係のインストール

```bash
pip install requests python-dotenv openai tree_sitter
```

### 4. GumTree CLI の設定（detectーcode/0_filtering.py 用）

`detectーcode/0_filtering.py` は差分解析に [GumTree](https://github.com/GumTreeDiff/gumtree) CLI を利用します。以下のいずれかの方法でパスを設定してください。

1. GumTree をインストールし、`gumtree` バイナリを PATH に追加する。
2. あるいは、環境変数 `GUMTREE_BIN` にフルパスを設定する（`.env` やシェルの `export` で設定可能）。

環境変数は `.env` 経由で読み込まれるため、以下のように追記すれば OK です。

```
GUMTREE_BIN=/path/to/gumtree
```

### 5. OpenAI API キー（detectーcode/1_dataset.py / 2_context.py / 3_prompt.py 用）

`detectーcode/1_dataset.py` と `detectーcode/2_context.py` は OpenAI Responses API を利用します。`detectーcode/3_prompt.py` でも `local = False` にした場合は同じ API を使用します。

1. [OpenAI Dashboard](https://platform.openai.com/settings/organization/api-keys) で API キーを作成
2. `.env` に以下を追記

```
OPENAI_API_KEY=your_openai_api_key_here
# Optional: モデルを上書きしたい場合
OPENAI_RESPONSES_MODEL=gpt-5.1
```

### 6. ローカル LLM 接続設定（detectーcode/3_prompt.py 用）

`detectーcode/3_prompt.py` で `local = True` の場合、ローカル LLM エンドポイントへ HTTP 経由で接続します。IP などの環境依存情報は `.env` に設定できます。

```
# 例: デフォルトから変更したい場合
LOCAL_LLM_HOST=192.168.1.20
LOCAL_LLM_PORT=1234
LOCAL_LLM_MODEL=qwen/qwen3-coder-480b
LOCAL_LLM_URL=http://192.168.1.20:1234/v1/chat/completions
```

`OPENAI_API_KEY` が設定されていない場合はスクリプトが起動時にエラーを出して終了します。

## detectーcode データ生成パイプライン

`detectーcode/` ディレクトリ配下には収集済みパッチを加工し、Few-shot CoT プロンプト用の修正例を構築するステップスクリプトが用意されています。

1. `0_filtering.py` … `../output` にある before/after ペアから対象 API（例: SwitchBot）を含む変更のみを抽出し、REST API らしい差分に絞り込みます。
    - 依存: GumTree CLI (`GUMTREE_BIN`) と tree-sitter 言語バイナリ。
2. `1_dataset.py` … 0番の出力を OpenAI Responses API で判定し、REST API 誤用修正だけを `1_dataset.jsonl` / `1_dataset.json` に保存します。
    - 依存: `OPENAI_API_KEY`、必要に応じて `OPENAI_RESPONSES_MODEL`。
3. **【手動】`1_dataset.json` の目視確認** … 各エントリの `commit_url` をブラウザで開き、実際のコミット内容と `code_before` / `code_after` を確認します。本当に REST API 誤用修正と判断できる事例には、対象エントリの `isEnabled` フィールドを `true` に書き換えます。
4. `2_context.py` … `isEnabled: true` のエントリのみを対象に、修正カテゴリ（エンドポイント・認証・HTTPメソッド等）に分類し、各修正例に CoT メッセージを付与したコンテキストを `2_context.jsonl` / `2_context.json` に生成します。
5. `3_prompt.py` … CoT 付き修正例を Few-shot 形式で埋め込んだプロンプトを構築し、ローカル LLM または OpenAI API に問い合わせることで、ユーザが指定したソースコード（`../dataset/<API名>/<ケースID>/misuse.<ext>`）の REST API バグを自動修正します。結果は `../result/` に保存されます。
    - ローカル実行時は `LOCAL_LLM_*` を設定し、OpenAI 実行時は `local = False` と API キーを用意してください。

## 使い方

### Step 1: コミットペアの収集

```bash
python get-code.py
```

キーワード（例: `switchbot`）を入力すると、以下の情報源から関連するコードの変更を収集し、`output/` に before/after ペアとして保存します：

- 関連リポジトリのPull Request
- コミットメッセージ
- PRタイトル

### Step 2〜4: 修正例の構築と自動バグ修正

`detectーcode/` ディレクトリに移動し、パイプラインを順に実行します：

```bash
cd detectーcode
python 0_filtering.py   # REST API 関連の修正ペアを抽出
python 1_dataset.py     # OpenAI API で誤用修正かどうかを判定
```

**【手動レビュー】** `1_dataset.json` を開き、各エントリの commit_url でコミット内容を確認します。REST API 誤用修正として適切と判断したエントリの `isEnabled` フィールドに `true` を記入してください。

```jsonc
// 1_dataset.json の例
{
  "comment": "エンドポイントの誤りを修正",
  "isEnabled": "true",   // ← 適切な事例に true を記入
  "type": "def_fix",
  ...
}
```

手動レビュー後、パイプラインの続きを実行します：

```bash
python 2_context.py     # カテゴリ分類 + CoT メッセージ付き修正例を生成
python 3_prompt.py      # Few-shot CoT プロンプトで LLM にバグ修正を依頼
```

`3_prompt.py` は `../dataset/` 以下に置いたユーザのソースコード（`misuse.<ext>`）を読み込み、`../result/` に修正済みコードを出力します。

## セキュリティ注意事項

### 機密情報の管理

- GitHub tokenは`.env`ファイルに保存し、**絶対にGitにコミットしない**
- `.env`ファイルは`.gitignore`で除外済み
- 生成された`output/`ディレクトリも`.gitignore`で除外済み

### トークンの権限

- 公開リポジトリのみを収集する場合は、最小限の権限（`public_repo`）のみを付与
- プライベートリポジトリへのアクセスが必要な場合のみ`repo`スコープを使用

### レート制限

- GitHub APIには[レート制限](https://docs.github.com/ja/rest/overview/resources-in-the-rest-api#rate-limiting)があります
- 認証済みの場合：5,000リクエスト/時間
- このツールはレート制限に達すると自動的に待機します

## 出力形式

```
output/
└── keyword(YYYYMMDDHHmm)/
    ├── Python/
    │   ├── patch0/
    │   │   ├── before.py
    │   │   ├── after.py
    │   │   ├── commit_url.txt
    │   │   └── file_name.txt
    │   └── patch1/
    │       └── ...
    ├── Java/
    └── JavaScript/
```

## トラブルシューティング

### "GITHUB_TOKEN が .env に設定されていません"

→ プロジェクトルートに`.env`ファイルを作成し、有効なGitHub tokenを設定してください。

### "レート制限に達しました"

→ 1時間待つか、別のGitHub tokenを使用してください。

### 403 Forbidden エラー

→ トークンの権限を確認するか、新しいトークンを生成してください。
