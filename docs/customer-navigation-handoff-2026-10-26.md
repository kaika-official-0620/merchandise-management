# お客様メニュー整理（2026-10-26）

## 変更

顧客のサイドメニューを「ホーム／商品・在庫／開花への依頼／売上・支払い／書類／お知らせ・サポート」の6入口へ集約。ホーム以外は説明付きの案内カードを表示する。履歴、CSV、通知・アカウント設定は用途に合う見出しで展開する。

- 旧商品一覧と新個品・作業記録、会社のお知らせと個別通知、既存の請求・精算書と新しい請求下書きを別々に保持。
- 各ページに重複していた全体ナビゲーションを顧客表示時だけ集約し、現在のカテゴリへ戻るリンクを追加。
- 旧代行仕入れの新規入口は案内せず、過去の取引履歴を保持。履歴からの戻り先も新しい依頼カテゴリへ変更。
- メニューに操作目的の説明、選択中の表示、未読書類数を維持。スマホではカードを1列表示。
- 画面端に触れただけで顧客メニューを開かない。閉じたメニューをキーボード・読み上げの対象外とし、開いたメニュー内のTab移動、Escapeで閉じる、フォーカス復帰を追加。
- 既存フラグ・契約権限を使用。制限中の顧客を新規預け入れの403画面へ案内せず、無料の自己登録と既存履歴を残す。月次請求のリンクはログイン本人のIDのみ。
- 管理者メニューは従来のものを使用。新コンテキストが未登録の旧起動経路では従来の顧客メニューへフォールバック。

## 変更ファイル

- `customer_navigation.py`、`render_app.py`の末尾登録
- `templates/base.html`、`templates/_sidebar_user.html`、`templates/_sidebar_user_legacy.html`
- `templates/customer_navigation/{hub,breadcrumb,icons}.html`
- `static/css/customer-navigation.css`
- `templates/{inventory_registry,finance_review}/frame.html`
- `templates/{workspace_home,commerce,payment_review}/base.html`
- `templates/proxy_service_history_public.html`
- `tests/test_customer_navigation.py`、`scripts/verify_customer_navigation.py`

既に存在した未コミット変更は保持しており、上記ファイル全体の差分が今回分とは限らない。DB・料金・取引内容を変更するマイグレーションはない。

## 確認した範囲

- 隔離Flask単体テスト15件成功：顧客限定、機能フラグ、権限、本人の請求URL、旧仕入れ新規入口の除外など。
- コピーした実Flask＋架空DB：163チェック成功、70 HTTP要求、5xx 0件。2顧客の全案内カード、他顧客の拒否、管理者メニュー維持を確認。
- 最終プレビューの既存起動検証33件成功。
- 旧仕入れ履歴テンプレートの顧客／従来表示／コンテキストなしの3ケース成功。
- ブラウザで6入口、カード遷移、現在地、設定の展開、閉じる操作、Escape、Tabの循環を確認。
- PC幅1280、スマホ幅390で表示確認。全5案内ページはスマホ幅で横方向のはみ出しなし。
- `git diff --check`成功（既存の改行コード注意表示はあり）。

実機iPhone/Android、ネイティブの再ビルド、Render反映、ストア配布は今回未実施。ブラウザの幅変更を実機検証とは扱わない。

## 確認用画面と証拠

- このPC専用：`http://127.0.0.1:18815/__preview/start/normal`。架空顧客でホームを開く。左上のメニューから新しい6入口へ進む。
- 起動コマンド：`python scripts/run_stage8_preview.py --port 18815`（既存の検証用Python依存関係を使用）。コピー・架空DB・外向き通信禁止の境界を使用。
- HTTP検証：`docs/verification-artifacts/customer-navigation-http-2026-10-26.json`
- 起動検証：`docs/verification-artifacts/customer-navigation-preview-2026-10-26.json`
- スクリーンショット：`C:/Users/leaps/.codex/visualizations/2026/09/09/01a084f6-f239-7610-a818-8914d1593b20/customer-navigation-20261026/`

以前の18814のプレビューは残し、今回18815を新設。本番・既存の顧客データ・実課金・外部通知は変更していない。確認用サーバー停止時はリンクを開けないため、同じコマンドで再起動が必要。

次はお客様によるメニュー名・分類の使い勝手の確認。その後の反映先や本番公開は別途確認する。アプリはWebView内でWeb画面を使うため、配信先更新後に実機でメニュー・戻る操作を再確認する。
