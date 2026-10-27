# PCに依存しない確認用サイトへの反映（2026-10-27）

## 対象と境界

- 利用者の依頼は、PCを閉じてもスマホ・別PCから最新の確認用サイトを利用すること。本番・実際の課金は変更しない。
- 対象は既存の `kaika-platform-staging`、サービスID `srv-daigtnfqj5pc73a2m7q0`、URL `https://kaika-platform-staging.onrender.com`。
- 専用PostgreSQLは `kaika-platform-staging-db`、DB名 `kaika_staging`。画像は既存5GB永続ディスク `/opt/render/project/src/static/uploads`。
- 調査時の稼働コミットは `a1afb1d103d50eb67288b70592d8b0f46a182572`、ブランチは `codex/kaika-platform-staging-20260912`、自動反映は Off。
- 本番ブランチ、本番DB、実在顧客データ、決済、通知配信、ストア配布は今回の対象外。

## ソース変更

- `staging_review.py`：検証専用起動で明示的に有効化された場合に限り、Stage2〜7の既存PostgreSQL定義を追加する。既存の業務行・写真・パスワードを上書きしない。既存商品を新規作業や課金明細へ変換しない。
- DB名・検証マーカー・永続ディスク・既存スキーマを事前に確認。既存関数やトリガーの相違は置換せず停止。DDLはトランザクション内、失敗時はロールバック。
- `staging_app.py`：既存の検証境界登録後に専用ブートストラップを呼ぶ。本番起動には追加しない。
- `staging_environment.py`：新しいStage6/9の資格情報と有効化フラグも検査し、実通知・Stripe接続・削除実行を遮断する。
- `render.staging.yaml`：`KAIKA_STAGING_REVIEW_ENABLED=1` と、外部配信・決済・削除の無効設定を明示。
- `scripts/package_staging_release.py`：DB・アップロード・秘密設定・Git履歴を含めず、ソースだけを新ディレクトリへ出力。出力側の `render.yaml` は検証専用定義。
- 新しいメニューはホーム／商品管理／開花への依頼／お金の確認／書類／ヘルプ。詳しくは `customer-navigation-handoff-2026-10-26.md`。

## 反映手順と復旧

1. 隔離したローカルTLS PostgreSQLで初回起動、再起動、顧客権限、最新ページを検証する。
2. 完成したソースを専用の新ブランチ `codex/kaika-cloud-review-20261027` に保存する。元の作業コピー・既存履歴は変更しない。このソース専用ブランチを本番へマージしない。
3. 既存の検証サービスだけでブランチと上記有効化フラグを変更し、テスト済みコミットを手動反映する。自動反映はOffを維持する。
4. 同じホストのHTTPS、検証マーカー、ログイン、最新のメニューとデータ保持を確認する。
5. 反映失敗時は前回の正常コミットへ戻す。追加した空テーブルは削除せず、既存データを保持する。既存Blueprintの同期で旧ブランチへ戻らないよう、同期前に対象差分を確認する。

## 未完了・留意点

- この記録作成時点では、新ソースのクラウド反映・反映後検証は未完了。結果は同ファイルへ追記する。
- パスワードは既存のRender秘密環境変数から供給し、再設定・チャット掲載をしない。
- 最新の記録画面を有効化するが、旧商品と新しい個品台帳の照合、契約・料金の確定、実課金、外部通知は未完了のまま区別する。
- これはブラウザで使う確認環境。iOS/Androidの正式配布・審査完了を意味しない。

## 検証記録

- `tests.test_staging_package`、`tests.test_staging_environment`、`tests.test_staging_review`、`tests.test_customer_navigation`、`tests.test_verify_deployed_staging`：合計86件成功（21.645秒）。
- 別担当による専用bootstrapの独立レビューで、具体的な重大欠陥は検出されなかった。実PostgreSQL検証は別項目として扱う。
- 反映前のRender Shellで、環境検証＋PostgreSQL読取専用セッションにより、専用DB一致、users 4件、merchandise 5件を確認。行内容や秘密値は出力していない。
- ソースパッケージ候補にはDB・アップロード・秘密設定を含めない。資格情報パターンの検査はテスト内の架空DSNだけに一致した。
