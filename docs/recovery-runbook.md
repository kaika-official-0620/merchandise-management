# 開花の全体保存・隔離復旧手順

更新: 2026-09-12

開花の在庫、受付・受領履歴、帳票、契約、ストアとの紐付け、通知情報は同じデータベースにあります。写真は `static/uploads` に保存されています。既存の Web 画面の JSON / ZIP は全業務データの復旧用ではありません。`backup_restore_guard.py` の制限は維持します。

`scripts/platform_recovery.py` を追加しました。全データベースと全写真をまとめて暗号化保存し、**起動していない専用の空の復旧先**にだけ復元する運用 CLI です。公開サービスの切替や上書き復元は行いません。

## 現時点の確認範囲

- 架空 SQLite の 16 テーブルと写真による全体保存・復元の往復を確認済み。自己保管・配送中・開花保管の区別、受付と商品 ID の関連、契約・ストア履歴、帳票、追加テーブル、画像、日本語ファイル名、連番、ビューを保持します。
- 保存元を変更せず、復元側のプッシュ端末を無効にし、未送信・送信中・受領確認待ちのキューを停止することを確認済み。
- 元と同じ接続先、本番と同じ URL、通常の `kaika_staging`、既存データ、画像破損、コード違い、危険なアーカイブ、秘密値のログ漏出を拒否する専用テストがあります。
- **PostgreSQL と age の実行試験は未実施。** この PC の PATH では `pg_dump` / `pg_restore` / `age` を確認できませんでした。PostgreSQL の CLI 引数と拒否条件はモックで確認しています。アーカイブ展開試験の age は架空の置換であり、暗号化の往復試験としては数えません。
- 本番 DB、実際の利用者の写真、`.env` は読み取っていません。本番の保存・復旧・停止・公開は行っていません。

リリース判定には、次に説明する本番と同じ PostgreSQL 世代・age・実際の保存先を使った**架空データの復旧演習**を通す必要があります。

## 保存するもの / 別管理するもの

保存するデータ:

1. PostgreSQL の全アプリケーション DB を custom 形式で保存します。特定テーブル・列の固定リストを使わないため、新しい受付・課金・通知テーブルも対象になります。
2. `static/uploads` 以下の全ファイルを保存します。写真以外の添付も含みます。シンボリックリンク、ジャンクション、ハードリンク、特殊ファイルは処理を止めます。
3. 作成日時、DB スキーマと件数、連番状態、コードと画面・公開素材の指紋、ファイルごとの SHA-256 とサイズを manifest に残します。manifest 自体も暗号化アーカイブ内に置きます。

DB の global role、所有者・ACL、tablespace、Render の設定、DNS、暗号鍵、API キー、アプリのビルド成果物はこのアーカイブから自動復元しません。復元先は専用の DB 所有者にし、`--no-owner --no-acl --no-tablespaces` で復元します。業務データの保存と権限・インフラ構築は分けて管理します。[PostgreSQL の保存範囲](https://www.postgresql.org/docs/current/app-pgdump.html)

`render.yaml` では写真ディスクの mount は `/opt/render/project/src/static/uploads`、サイズ指定は 200 GB です。これは構成ファイルの値であり、現在契約中の Render の実設定は未確認です。Render のディスクは mount 以下だけが永続化されるため、DB と写真の両方を保存します。[Render の永続ディスク](https://render.com/docs/disks)

## 初回だけ運用担当者が準備するもの

1. 管理用の保護された作業環境。Python とアプリの依存関係、ソース DB と**同じメジャー版**の `pg_dump` / `pg_restore`、公式の `age` / `age-keygen` を用意します。CLI は世代が違う DB への移行を兼ねません。[age 公式](https://github.com/FiloSottile/age)
2. age の X25519 鍵を用意します。保存作業には公開 recipient だけを渡します。復号用 identity はパスワード管理庫などに分離し、バックアップファイル・コード・OneDrive 共有フォルダには同梱しません。鍵紛失に備え、権限を限定した別の保管担当・復旧手段も用意します。
3. 暗号化された作業ボリュームと保存先。Windows は作業フォルダの ACL も管理者だけにします。作業中の DB dump、コピーした写真、tar は平文です。通常終了・失敗時には、この CLI が作った一時子フォルダだけを削除します。強制終了・停電時には残る可能性があります。コードだけで暗号化ディスクや Windows ACL を設定したとはみなしません。
4. 保存先は本番サービスと独立し、履歴・アクセス制限のある場所にします。最終 `.tar.age` と出力された `backup_sha256` を別の信頼できる台帳にも記録します。復旧時は、その台帳の digest を必須にします。
5. バックアップ時点のソースコード・依存関係をリリース単位で保管します。CLI の指紋はコードを含むバックアップの代わりではありません。復元は同じコードで行い、アップデートは復元確認後の別作業です。

作業容量は、保存時に DB dump と全写真のコピー、平文 tar、暗号化ファイルが同時に存在します。復元時にも平文 tar、展開内容、復元先が必要です。写真容量を確認し、**対象全体の概ね 3 倍以上と DB・作業分の余裕**を見込んだ専用ボリュームを用意します。通常のアプリディスクを一時作業領域に流用しません。

## 保存の手順

1. 作業の承認済み範囲と日時、復旧対象のリリースを記録します。初回は架空データだけの専用サービスで演習します。
2. 保存元への**すべての書き込みを止めます**。利用者・管理者からの登録だけでなく、webhook、定期課金/移管処理、通知 worker、取り込み処理、添付更新も対象です。CLI はサービスを停止しません。`--all-writers-stopped` は作業者による確認の表明であり、停止を自動検出したことにはなりません。
3. 管理端末の秘密環境変数に接続 URL を設定します。チャット、コマンド履歴、ソース、ログへ URL のパスワードを貼り付けません。CLI に渡すのは環境変数の**名前**です。`DATABASE_URL` のようなアプリの変数を自動で拾う仕組みはありません。
4. 以下を実行します。パス・公開 recipient は作業環境の実値に置き換えます。例は Linux の管理端末向けです。

```bash
python scripts/platform_recovery.py backup \
  --kind postgres \
  --database-env RECOVERY_SOURCE_DATABASE_URL \
  --source-origin-env RECOVERY_SOURCE_APP_ORIGIN \
  --uploads /opt/render/project/src/static/uploads \
  --code-dir /opt/render/project/src \
  --work-root /private-recovery/work \
  --output /private-backups/kaika-release-backup.tar.age \
  --recipient AGE_PUBLIC_RECIPIENT \
  --all-writers-stopped
```

`RECOVERY_SOURCE_APP_ORIGIN` は元サービスの `https://...` だけにし、画面パス・クエリ・認証情報を入れません。DB URL は単一ホストの直結 PostgreSQL URL に限定しています。TLS は必須で、URL に指定がなければ `sslmode=require` になります。複数ホスト・service 経由・任意の session override は受け付けません。

DB 件数・スキーマを取得するトランザクションの snapshot を `pg_dump` と共有します。写真コピー後には全ファイルの再ハッシュと一覧比較を行い、途中の変更を検出したら中止します。ただし DB とファイルシステムを跨ぐ原子的な保存ではないため、書き込み停止は省略できません。

成功時だけ `encrypted-backup-created`、テーブル数、添付数、暗号化アーカイブの `backup_sha256` が出ます。その digest と保存日時・リリース・作業者を台帳へ記録します。終了コードが 0 以外、または中断したファイルは成功した保存として扱いません。元サービスを再開する前に、暗号化ファイルが保存され、鍵の所在が確認できることを確かめます。

## 空の隔離先へ復元する手順

### 復旧先の条件

- DB 名は `kaika_recovery_` に小文字英数字 8～40 文字を付けた専用名。例: `kaika_recovery_a1b2c3d4`。`kaika_staging` や本番 DB への復元は拒否します。
- 元と同じ DB 名は、ホスト名・資格情報が違っても拒否します。別名のホスト経由で同じ DB を指定する誤りも防ぎます。
- 復元先は新しい DB。既存テーブルだけでなく、ビュー、連番、関数、独自型、独自スキーマ・拡張がある場合も中止します。標準の `public` スキーマと `plpgsql` は許容します。
- 写真の復元先は作業者が明示して作成した空ディレクトリ。元の写真保存先や既存画像への上書きをしません。
- 復旧用 URL は元の URL と本番 URL の両方と別です。HTTPS origin の正規化後に比較します。CLI はその URL に HTTP 接続しません。
- **この DB・ディスクに接続する Web アプリ、worker、cron、webhook は起動しないでください。** 通常の staging に差し込んで起動したり、利用者ログインやテストユーザー seed を行ったりしません。
- 復旧作業環境に本番の LINE / Expo / Stripe / ストア API の送信資格情報を入れません。DB への接続以外の外部接続も、実行環境側で遮断します。

### 復元コマンド

```bash
python scripts/platform_recovery.py restore \
  --backup /private-backups/kaika-release-backup.tar.age \
  --expected-backup-sha256 TRUSTED_INVENTORY_SHA256 \
  --identity-file /private-keys/kaika-backup.agekey \
  --database-env RECOVERY_TARGET_DATABASE_URL \
  --production-origin-env RECOVERY_PRODUCTION_APP_ORIGIN \
  --target-origin-env RECOVERY_TARGET_APP_ORIGIN \
  --uploads /private-recovery-target/uploads \
  --code-dir /private-releases/exact-backup-release \
  --work-root /private-recovery/work \
  --report /private-recovery-reports/first-restoration.json \
  --max-bytes 1073741824 \
  --all-writers-stopped \
  --isolated-target-confirmed
```

`--max-bytes` は manifest と DB と添付の展開サイズ上限です。例は 1 GiB の架空演習用で、本番容量を示す値ではありません。実データ量に合わせて管理者が上限を決めます。age はこの確認前に tar を復号するため、ディスク容量や OS 側の上限も別途必要です。

CLI はまず台帳の SHA-256、復号、アーカイブ内のパスと重複・リンク、manifest の整合、リリースの一致、復元先条件を確認します。`pg_restore` は `--single-transaction --exit-on-error` で実行し、`--clean` / `--create` は使いません。DB を復元した後にスキーマ・各テーブルの件数・連番状態を比較します。[PostgreSQL の復元オプション](https://www.postgresql.org/docs/current/app-pgrestore.html)

次に復元先だけで `push_devices.enabled=0` にし、`push_outbox.state` が `queued` / `receipt` / `sending` の行を `cancelled` にします。送信済み・失敗履歴、業務データ、契約情報、元の `kaika_environment` marker は保持します。新たに `kaika_recovery_quarantine` テーブルを作り、隔離状態・バックアップの指紋・復元日時を記録します。

最後に写真をコピーし、各ファイルの SHA-256 を確認します。成功レポートは `quarantined-not-activated` です。**これは公開可能・業務再開済みという意味ではありません。** 元のプッシュ端末は再登録が必要になり、過去の通知を再送しません。

PostgreSQL の論理 subscription、foreign server、event trigger、標準外 extension がある DB は停止します。DB 自体が復元時に外部通信・自動ジョブを起動する可能性を、今回の一般アプリ用手順で引き継がないためです。必要な場合は、その機能を含めた別の復旧設計・演習が必要です。

## 失敗時 / 再開前

DB と写真を跨いで全体を巻き戻す仕組みはありません。DB 復元後に写真コピーが失敗した場合などは、**部分復元先を起動せず、隔離したまま**にします。同じ先への上書き再試行は拒否されます。復旧担当者が原因と安全性を確認してから、新しい空の専用 DB・空ディレクトリを用意します。CLI は既存 DB・既存写真を消してやり直しません。

本番切替は別の作業として、少なくとも次を確認します。

- 業務担当者による対象商品・受付・売上・帳票の照合。ファイルハッシュ・行数の一致だけで業務の意味までは判断しません。
- 保存時点以降の業務記録・書き込みとの差分。必要な回復範囲と停止時間を決めます。
- Stripe / Apple / Google など外部サービスに保存されている現在の契約状態との再照合。バックアップ時点へ戻した契約情報だけで二重請求・権限再付与を判断しません。
- LINE・プッシュ・webhook・定期処理の再開順序と重複防止。通知資格情報は照合が終わってから設定し、限定したテストを行います。
- 新しい `SECRET_KEY` などの設定、利用者再認証、権限・ドメイン・ストレージ参照先の確認。隔離 marker の解除や本番環境 marker の変更は、この CLI では行いません。

## 検証の再実行

```bash
python -m unittest discover -s tests -p test_platform_recovery.py -v
```

テストはアプリを import せず、使い捨ての架空 DB・写真とモックだけを使います。SQLite は DB の親に `.kaika-recovery-fixture` と正しい架空データ用ラベルがある場合しか扱いません。通常運用の SQLite 復旧をサポートしたものではありません。

次の運用演習では、架空データを PostgreSQL に投入し、age 公開鍵で保存 → 分離した秘密鍵で復号 → 新しい専用 PostgreSQL へ復旧 → レポートと業務値照合まで確認します。所要時間・容量を測り、保存頻度、保持期間、許容するデータ損失時間と復旧時間を決定します。本番データの復旧演習は、その後に権限と対象を明示して行います。
