# 公開検証環境の PC・アプリ相当連動確認

`scripts/verify_deployed_staging.py` は **kaika-platform-staging の Render Shell** で実行する確認用 CLI です。専用 staging 設定、PostgreSQL 接続先の DB 名、写真ディスクの既存 staging マーカーを先に確認します。対象は `https://kaika-platform-staging.onrender.com` に固定しています。

最初の確認は次のコマンドで行います。

```sh
python scripts/verify_deployed_staging.py --with-intake
```

環境変数にある検証用パスワードを内部で使用します。パスワードをコマンド引数に書いたり、環境変数を表示したりする必要はありません。各セッションで匿名のトップページ、`/healthz`、ログイン画面を取得し、全応答の `X-Kaika-Environment: staging`、ログイン画面の検証表示、health の PostgreSQL・ホスト名一致を確認してからログインします。TLS 証明書検証を有効にし、プロキシの継承、自動リダイレクト、他 origin へのアクセスを許可しません。

この実行は、商品名が `【動作確認・架空】` とランダムな識別子の **架空商品を新規に 1 点** 作成します。`staging_business` の PC 相当と KaikaApp 相当の独立した cookie セッションで、次を確認します。

- PC 側の写真付き登録をアプリ側から開ける。
- アプリ側の編集が同じ商品 ID で PC 側に反映され、古い編集画面からの上書きが拒否される。
- 別の検証利用者 `staging_normal` から商品情報を読めない。
- PC・アプリ側で写真データが一致する。
- 同じ新規商品の預け入れを申請し、`staging_admin` の発送案内、利用者の架空の発送報告、管理者の架空の受領処理が反映される。
- 受領後も商品 ID・所有者の閲覧権限・写真が維持され、利用者側からの編集が止まる。
- アプリ側のログアウトが PC 側のログイン状態に影響しない。

預け入れを含めない最小確認は、`--with-intake` を付けずに実行します。いずれも既存の商品・受付は変更しません。実物の発送、外部通知、決済、商品削除は行いません。作成した架空の商品・受付は確認用として残ります。通常の実行を繰り返すと、そのたびに別の商品が 1 点作成されます。

## 再起動後の保全確認

最初の成功結果の `run_id` を控え、Render のサービス再起動が完了した後、**同じサービスの Shell** で次を実行します。`<run_id>` は出力された 32 桁の英数字に置き換えます。

```sh
python scripts/verify_deployed_staging.py --verify-run <run_id>
```

このモードは在庫・受付を変更せず、同じ商品の情報、保管状態、写真の保存先・SHA256、所有者の権限、受領済み受付を再確認します。ログイン処理に伴うログイン履歴とセッション情報は更新されます。空文字や不正な識別子は通信前に拒否され、新規作成へ切り替わることはありません。

照合に使う小さな署名付き記録は、専用写真ディスクの `.staging-verification/<run_id>.json` に保存します。記録には架空の商品 ID、名称、写真の保存先と SHA256、受付 ID、識別子、環境名だけが含まれます。パスワード・セッション・CSRF トークンは保存しません。署名は staging の `SECRET_KEY` で検証するため、このキーを変更した場合は以前の記録による照合を拒否します。

## 結果の読み方と確認範囲

`DEPLOYED_STAGING_JSON=` に続く JSON の `failed: 0` が全確認成功です。`run_id`、`item_id`、`intake_id`、`photo_sha256` は架空データの照合用として共有できます。応答本文、認証情報、URL のクエリは出力しません。失敗時には停止した工程と固定のエラーコードだけを出力します。HTTP 応答が失われた場合は作成の有無を確定できないため、`created_item` などが `unknown_until_response` になることがあります。

ローカル検証では、専用の一時コピーと架空 SQLite を使い、実際の `render_app` のルートで新規登録から受領まで 40 項目、在庫を変更しない再照合で 20 項目が成功しました。通信用のテストアダプターを使用しており、health の DB 種別だけを明示的に置換しています。このローカル結果は実 Render の HTTPS・PostgreSQL・ディスクや実際のサービス再起動の確認を意味しません。

安全性の単体テストは 16 件です。再実行する場合はプロジェクトの依存ライブラリがある環境で次を使います。

```sh
python -m unittest discover -s tests -p test_verify_deployed_staging.py -v
python tests/test_verify_deployed_staging.py --runtime-child
```

実サービスで成功した場合でも、これは PC・アプリ相当の HTTP セッションの業務連動確認です。iPhone の WebView、カメラ、プッシュ通知、アプリ内購入、ストア審査は別途確認が必要です。
