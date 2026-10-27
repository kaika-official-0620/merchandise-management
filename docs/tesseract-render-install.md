# Render の Tesseract 取得先修正（2026-09-12）

新規 staging のビルドで `/var/lib/apt/lists/partial` が読み取り専用となり、パッケージ一覧を取得できず失敗しました。

`scripts/install_tesseract_render.sh` は、既存の Tesseract が `eng` / `jpn` に対応していればそのまま終了します。それ以外は、システムへの `apt-get install` を試さず、既存の `.deb` ダウンロード・ユーザー領域への展開を使います。

APT の共通オプションを `update`・`apt-cache depends`・`download` のすべてに渡します。

- パッケージ一覧: `$PWD/.render/apt-state/lists`（`partial` を事前作成）
- キャッシュ・アーカイブ: `$PWD/.render/apt-cache`
- ログ: `$PWD/.render/apt-log`

システムのリポジトリ設定・鍵束は参照し、署名検証を維持します。`sudo`、`--allow-unauthenticated`、不正署名・期限検査の無効化は追加していません。取得した index の更新や依存解決に失敗した場合は、途中で中止します。APT の設定名と共通 `-o` の指定は [Debian の apt.conf](https://manpages.debian.org/bookworm/apt/apt.conf.5.en.html) に従っています。`apt-get download` は通常の認証を維持し、カレントディレクトリへ取得します。[apt-get の説明](https://manpages.debian.org/bookworm/apt/apt-get.8.en.html)

展開先 `.render/tesseract`、`RENDER_TESSERACT_ROOT`、実行時の PATH / LD_LIBRARY_PATH / TESSDATA_PREFIX の扱いは従来どおりです。

検証: `python -m unittest discover -s tests -p test_tesseract_render_installer.py -v` **6 件成功**。Bash 構文、3 種の APT コマンドが同じ専用 index を参照すること、失敗時の中止、既存 Tesseract の早期終了、言語不足の検出を確認しました。テストは fake コマンドだけを使用し、システム APT やネットワークを操作しません。実際の Render ビルド成功は再デプロイで確認します。
