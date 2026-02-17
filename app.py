# -*- coding: utf-8 -*-
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, jsonify, make_response
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
import os
import io
import csv
import json
import zipfile
import shutil
import tempfile
import time
import calendar
from datetime import datetime, timedelta
from urllib.parse import urlparse

# Stripe連携
try:
    import stripe
    STRIPE_ENABLED = True
except ImportError:
    STRIPE_ENABLED = False

# Stripe設定
STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')

# Stripe料金プラン（Price ID）- 各月額料金に対応
# Stripeダッシュボードで作成したPriceのIDを設定
STRIPE_PRICE_IDS = {
    2500: os.environ.get('STRIPE_PRICE_2500', ''),    # ¥2,500/月 (0-20件)
    5000: os.environ.get('STRIPE_PRICE_5000', ''),    # ¥5,000/月 (21-50件)
    10000: os.environ.get('STRIPE_PRICE_10000', ''),  # ¥10,000/月 (51-100件)
    20000: os.environ.get('STRIPE_PRICE_20000', ''),  # ¥20,000/月 (101-200件)
    30000: os.environ.get('STRIPE_PRICE_30000', ''),  # ¥30,000/月 (201-300件、300件超は要相談)
}

if STRIPE_ENABLED and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# Google Drive API設定
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_DRIVE_ENABLED = bool(GOOGLE_API_KEY and GOOGLE_CLIENT_ID)

# APScheduler（月末自動処理用）
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    SCHEDULER_ENABLED = True
except ImportError:
    SCHEDULER_ENABLED = False

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'merchandise-management-secret-key-2024')

# グローバルエラーハンドラー（エラーをブラウザに詳細表示）
@app.errorhandler(500)
def internal_error(error):
    import traceback
    error_details = traceback.format_exc()
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>500 Internal Server Error</title>
        <style>
            body {{ font-family: monospace; padding: 20px; background: #1a1a2e; color: #eee; }}
            h1 {{ color: #e94560; }}
            pre {{ background: #16213e; padding: 15px; border-radius: 8px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; }}
            .error-type {{ color: #f39c12; }}
        </style>
    </head>
    <body>
        <h1>500 Internal Server Error</h1>
        <p class="error-type">Error: {error}</p>
        <h3>Traceback:</h3>
        <pre>{error_details}</pre>
    </body>
    </html>
    '''
    return html, 500

@app.errorhandler(Exception)
def handle_exception(e):
    import traceback
    error_details = traceback.format_exc()
    html = f'''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Error - {type(e).__name__}</title>
        <style>
            body {{ font-family: monospace; padding: 20px; background: #1a1a2e; color: #eee; }}
            h1 {{ color: #e94560; }}
            pre {{ background: #16213e; padding: 15px; border-radius: 8px; overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; }}
            .error-type {{ color: #f39c12; font-size: 1.2em; }}
            .error-msg {{ color: #fff; margin: 10px 0; }}
        </style>
    </head>
    <body>
        <h1>Application Error</h1>
        <p class="error-type">{type(e).__name__}</p>
        <p class="error-msg">{str(e)}</p>
        <h3>Traceback:</h3>
        <pre>{error_details}</pre>
    </body>
    </html>
    '''
    return html, 500

# Flask-Login設定
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'ログインが必要です'

# ファイルアップロード設定
# Render.com環境では永続ディスクのマウントポイントを使用
if os.environ.get('RENDER'):
    UPLOAD_FOLDER = '/opt/render/project/src/static/uploads'
else:
    # ローカル環境
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB（バックアップファイル用）

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# テンプレートにnow関数を追加
@app.context_processor
def inject_now():
    return {'now': datetime.now}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# データベース設定（PostgreSQL or SQLite）
DATABASE_URL = os.environ.get('DATABASE_URL')

# 起動時デバッグ: 環境変数の状態を出力（センシティブな情報は隠す）
print("=" * 60)
print("[STARTUP] Environment Check")
print(f"[INFO] DATABASE_URL is set: {DATABASE_URL is not None}")
if DATABASE_URL:
    # URLスキームのみ表示（ユーザー名やパスワードは隠す）
    print(f"[INFO] Database type: {'PostgreSQL' if DATABASE_URL.startswith('postgres') else 'Other'}")
print(f"[INFO] RENDER environment: {os.environ.get('RENDER', 'false')}")
print("=" * 60)

if DATABASE_URL:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    
    def get_db():
        url = urlparse(DATABASE_URL)
        conn = psycopg2.connect(
            host=url.hostname,
            port=url.port,
            database=url.path[1:],
            user=url.username,
            password=url.password,
            sslmode='require'
        )
        return conn
    
    def init_db():
        conn = get_db()
        # autocommitモードを有効化（ALTER TABLEの失敗が他のコマンドに影響しないように）
        conn.autocommit = True
        cur = conn.cursor()
        
        # ユーザーテーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(80) UNIQUE NOT NULL,
                email VARCHAR(120) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(20) DEFAULT 'user',
                display_name VARCHAR(100),
                admin_permissions TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP
            )
        ''')
        
        # admin_permissionsカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS admin_permissions TEXT")
        except:
            pass
        
        # Stripe関連カラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR(100)")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR(100)")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status VARCHAR(50) DEFAULT 'inactive'")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_payment_date TIMESTAMP")
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS next_payment_date TIMESTAMP")
        except:
            pass
        
        # 代行仕入れサービス利用可能金額カラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS proxy_service_budget INTEGER DEFAULT 0")
            print("[DEBUG] proxy_service_budget カラム追加成功/既存")
        except Exception as e:
            print(f"[ERROR] proxy_service_budget カラム追加失敗: {e}")
        
        # 月謝免除フラグカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tuition_exempt BOOLEAN DEFAULT FALSE")
            print("[DEBUG] tuition_exempt カラム追加成功/既存")
        except Exception as e:
            print(f"[ERROR] tuition_exempt カラム追加失敗: {e}")
        
        # 未払い開始日カラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS overdue_since TIMESTAMP")
        except:
            pass
        
        # オーナーがいない場合、最初の管理者をオーナーに昇格
        try:
            cur.execute("SELECT COUNT(*) FROM users WHERE role = 'owner'")
            owner_count = cur.fetchone()[0]
            if owner_count == 0:
                cur.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
                admin_row = cur.fetchone()
                if admin_row:
                    cur.execute("UPDATE users SET role = 'owner' WHERE id = %s", (admin_row[0],))
        except Exception as e:
            pass  # テーブルがまだ存在しない場合はスキップ
        
        # 商品テーブル（user_id追加）
        cur.execute('''
            CREATE TABLE IF NOT EXISTS merchandise (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                purchase_date DATE,
                photo_path TEXT,
                product_name VARCHAR(200),
                brand_name VARCHAR(100),
                item_condition VARCHAR(10),
                store_name VARCHAR(200),
                purchase_price INTEGER DEFAULT 0,
                payment_method VARCHAR(50),
                listing_price INTEGER DEFAULT 0,
                expected_shipping INTEGER DEFAULT 0,
                expected_commission INTEGER DEFAULT 0,
                is_listed BOOLEAN DEFAULT FALSE,
                listing_date DATE,
                sale_date DATE,
                sale_type VARCHAR(50) DEFAULT 'normal',
                sale_price INTEGER DEFAULT 0,
                shipping_cost INTEGER DEFAULT 0,
                sales_destination VARCHAR(100),
                commission INTEGER DEFAULT 0,
                is_shipped BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # brand_nameカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS brand_name VARCHAR(100)")
        except:
            pass
        
        # item_conditionカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS item_condition VARCHAR(10)")
        except:
            pass
        
        # additional_photosカラムを追加（複数画像対応）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS additional_photos TEXT")
        except:
            pass
        
        # sale_typeカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS sale_type VARCHAR(50) DEFAULT 'normal'")
        except:
            pass
        
        # model_numberカラムを追加（型番）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS model_number VARCHAR(100)")
        except:
            pass
        
        # kaika_product_codeカラムを追加（開花商品番号）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS kaika_product_code VARCHAR(100)")
        except:
            pass
        
        # supplier_detailカラムを追加（仕入先詳細）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS supplier_detail VARCHAR(50)")
        except:
            pass
        
        # id_document_pathカラムを追加（身分証）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS id_document_path TEXT")
        except:
            pass
        
        # consent_form_pathカラムを追加（同意書）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS consent_form_path TEXT")
        except:
            pass
        
        # updated_byカラムを追加（最終更新者）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS updated_by INTEGER REFERENCES users(id)")
        except:
            pass
        
        # updated_atカラムを追加（最終更新日時）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP")
        except:
            pass
        
        # notesカラムを追加（備考・メモ）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS notes TEXT")
        except:
            pass
        
        # 商品処分申請テーブル（merchandiseテーブルの後に作成）
        cur.execute('''
            CREATE TABLE IF NOT EXISTS item_disposal_requests (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                merchandise_id INTEGER REFERENCES merchandise(id),
                disposal_type VARCHAR(30) NOT NULL,
                reason VARCHAR(30) DEFAULT 'overdue',
                shipping_address TEXT,
                shipping_name TEXT,
                shipping_phone TEXT,
                status VARCHAR(20) DEFAULT 'pending',
                admin_note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                processed_by INTEGER REFERENCES users(id)
            )
        ''')
        
        # reasonカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE item_disposal_requests ADD COLUMN IF NOT EXISTS reason VARCHAR(30) DEFAULT 'overdue'")
        except:
            pass
        
        # 顧客テーブル（user_id追加）
        cur.execute('''
            CREATE TABLE IF NOT EXISTS customers (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                name VARCHAR(100) NOT NULL,
                email VARCHAR(120),
                phone VARCHAR(20),
                address TEXT,
                total_purchase INTEGER DEFAULT 0,
                purchase_count INTEGER DEFAULT 0,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # お知らせテーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS announcements (
                id SERIAL PRIMARY KEY,
                title VARCHAR(200) NOT NULL,
                content TEXT NOT NULL,
                announcement_type VARCHAR(20) DEFAULT 'info',
                is_active BOOLEAN DEFAULT TRUE,
                publish_at TIMESTAMP,
                expire_at TIMESTAMP,
                created_by INTEGER REFERENCES users(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # ウィジェット設定テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS widget_settings (
                id SERIAL PRIMARY KEY,
                widget_key VARCHAR(50) UNIQUE NOT NULL,
                widget_name VARCHAR(100) NOT NULL,
                is_enabled BOOLEAN DEFAULT TRUE,
                display_order INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # デフォルトウィジェット設定を挿入
        default_widgets = [
            ('sales_profit', '売上・利益', True, 1),
            ('top_products', '売れた商品', True, 2),
            ('slow_products', '売れない商品', True, 3),
            ('turnover_rate', '回転率・在庫日数', True, 4),
            ('closing_rate', '成約率', True, 5),
            ('avg_price', '平均単価', True, 6),
            ('repeat_rate', 'リピート率', False, 7),
            ('time_sales', '時間帯・曜日別売上', False, 8),
            ('brand_stats', 'ブランド別統計', True, 9),
            ('destination_stats', '販売先別統計', True, 10),
        ]
        for widget in default_widgets:
            cur.execute('''
                INSERT INTO widget_settings (widget_key, widget_name, is_enabled, display_order)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (widget_key) DO NOTHING
            ''', widget)
        
        # 精算書テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS shikiriosho (
                id SERIAL PRIMARY KEY,
                document_no VARCHAR(50) NOT NULL,
                sender_id INTEGER REFERENCES users(id),
                recipient_id INTEGER REFERENCES users(id),
                recipient_name VARCHAR(100),
                issue_date DATE NOT NULL,
                due_date DATE,
                subtotal INTEGER DEFAULT 0,
                tax_amount INTEGER DEFAULT 0,
                total_amount INTEGER DEFAULT 0,
                tax_rate DECIMAL(5,2) DEFAULT 10.0,
                notes TEXT,
                status VARCHAR(20) DEFAULT 'draft',
                is_read INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 精算書明細テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS shikiriosho_items (
                id SERIAL PRIMARY KEY,
                shikiriosho_id INTEGER REFERENCES shikiriosho(id) ON DELETE CASCADE,
                item_no INTEGER NOT NULL,
                product_name VARCHAR(200) NOT NULL,
                specification VARCHAR(200),
                quantity INTEGER DEFAULT 1,
                unit_price INTEGER DEFAULT 0,
                amount INTEGER DEFAULT 0,
                notes TEXT
            )
        ''')
        
        # shikirioshoにcontact_name, personal_numberカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE shikiriosho ADD COLUMN IF NOT EXISTS contact_name VARCHAR(100)")
            cur.execute("ALTER TABLE shikiriosho ADD COLUMN IF NOT EXISTS personal_number VARCHAR(50)")
        except:
            pass
        
        # shikiriosho_itemsにproduct_date, product_codeカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE shikiriosho_items ADD COLUMN IF NOT EXISTS product_date DATE")
            cur.execute("ALTER TABLE shikiriosho_items ADD COLUMN IF NOT EXISTS product_code VARCHAR(50)")
        except:
            pass
        
        # 買取明細書テーブル（ユーザー→管理者）
        cur.execute('''
            CREATE TABLE IF NOT EXISTS invoices (
                id SERIAL PRIMARY KEY,
                invoice_no VARCHAR(50) NOT NULL,
                sender_id INTEGER REFERENCES users(id),
                issue_date DATE NOT NULL,
                payment_due_date DATE,
                recipient_name VARCHAR(200),
                postal_number VARCHAR(20),
                subtotal INTEGER DEFAULT 0,
                tax_amount_8 INTEGER DEFAULT 0,
                tax_amount_10 INTEGER DEFAULT 0,
                total_amount INTEGER DEFAULT 0,
                service_type VARCHAR(50) DEFAULT 'normal',
                commission_rate DECIMAL(5,2) DEFAULT 10.00,
                commission_amount INTEGER DEFAULT 0,
                bank_info TEXT,
                notes TEXT,
                status VARCHAR(20) DEFAULT 'draft',
                is_read INTEGER DEFAULT 0,
                approved_at TIMESTAMP,
                approved_by INTEGER REFERENCES users(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # service_type, commission_rate, commission_amountカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS service_type VARCHAR(50) DEFAULT 'normal'")
            cur.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS commission_rate DECIMAL(5,2) DEFAULT 10.00")
            cur.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS commission_amount INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS recipient_name VARCHAR(100)")
            cur.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS postal_number VARCHAR(20)")
        except:
            pass
        
        # 買取明細書明細テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS invoice_items (
                id SERIAL PRIMARY KEY,
                invoice_id INTEGER REFERENCES invoices(id) ON DELETE CASCADE,
                item_no INTEGER NOT NULL,
                tax_category VARCHAR(10) DEFAULT '10',
                product_date DATE,
                product_name VARCHAR(200) NOT NULL,
                quantity INTEGER DEFAULT 1,
                unit VARCHAR(20),
                unit_price INTEGER DEFAULT 0,
                amount INTEGER DEFAULT 0
            )
        ''')
        
        # invoice_itemsにproduct_codeカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE invoice_items ADD COLUMN IF NOT EXISTS product_code VARCHAR(50)")
        except:
            pass
        
        # サービス書類テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS service_documents (
                id SERIAL PRIMARY KEY,
                document_no VARCHAR(50) NOT NULL,
                user_id INTEGER REFERENCES users(id),
                service_type VARCHAR(50) NOT NULL,
                customer_name VARCHAR(100) NOT NULL,
                contact VARCHAR(200),
                product_name VARCHAR(200) NOT NULL,
                product_description TEXT,
                quantity INTEGER DEFAULT 1,
                unit_price INTEGER DEFAULT 0,
                commission INTEGER DEFAULT 0,
                total_amount INTEGER DEFAULT 0,
                service_data TEXT,
                notes TEXT,
                status VARCHAR(20) DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 代行仕入れサービス設定テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS proxy_service_settings (
                id SERIAL PRIMARY KEY,
                is_public BOOLEAN DEFAULT FALSE,
                page_title VARCHAR(200) DEFAULT '代行仕入れサービス',
                page_description TEXT,
                start_datetime TIMESTAMP,
                end_datetime TIMESTAMP,
                updated_by INTEGER REFERENCES users(id),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 公開日時カラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE proxy_service_settings ADD COLUMN IF NOT EXISTS start_datetime TIMESTAMP")
            cur.execute("ALTER TABLE proxy_service_settings ADD COLUMN IF NOT EXISTS end_datetime TIMESTAMP")
        except:
            pass
        
        # 販売方式カラムを追加（既存テーブル用） auction=オークション, fixed=即決
        try:
            cur.execute("ALTER TABLE proxy_service_settings ADD COLUMN IF NOT EXISTS sale_mode VARCHAR(20) DEFAULT 'auction'")
        except:
            pass
        
        # オークション名カラムを追加（複数オークション対応）
        try:
            cur.execute("ALTER TABLE proxy_service_settings ADD COLUMN IF NOT EXISTS auction_name VARCHAR(100) DEFAULT 'オークション'")
        except:
            pass
        
        # 商品にオークションIDを追加（複数オークション対応）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS auction_id INTEGER REFERENCES proxy_service_settings(id)")
        except:
            pass
        
        # 計算書に管理者作成フラグを追加（オークション落札用）
        try:
            cur.execute("ALTER TABLE user_keisan ADD COLUMN IF NOT EXISTS is_admin_created BOOLEAN DEFAULT FALSE")
        except:
            pass
        
        # 代行サービス公開ユーザーテーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS proxy_service_users (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                is_enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id)
            )
        ''')
        
        # 入札テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS proxy_service_bids (
                id SERIAL PRIMARY KEY,
                merchandise_id INTEGER REFERENCES merchandise(id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES users(id),
                bidder_name VARCHAR(100) NOT NULL,
                bid_amount INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # user_idカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE proxy_service_bids ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)")
        except:
            pass
        
        # 商品に代行サービス表示フラグを追加
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS show_in_proxy_service BOOLEAN DEFAULT FALSE")
        except:
            pass
        
        # デフォルト設定を挿入
        cur.execute("SELECT COUNT(*) FROM proxy_service_settings")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO proxy_service_settings (is_public) VALUES (FALSE)")
        
        # LINE連携設定テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS line_settings (
                id SERIAL PRIMARY KEY,
                channel_access_token TEXT,
                channel_secret VARCHAR(100),
                is_enabled BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # LINE定期送信メッセージテーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS line_scheduled_messages (
                id SERIAL PRIMARY KEY,
                title VARCHAR(200) NOT NULL,
                message_content TEXT NOT NULL,
                schedule_type VARCHAR(20) DEFAULT 'daily',
                schedule_time TIME,
                schedule_day INTEGER,
                target_type VARCHAR(20) DEFAULT 'all',
                report_type VARCHAR(30),
                is_enabled BOOLEAN DEFAULT TRUE,
                last_sent_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # report_typeカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE line_scheduled_messages ADD COLUMN IF NOT EXISTS report_type VARCHAR(30)")
        except:
            pass
        
        # LINE送信履歴テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS line_message_logs (
                id SERIAL PRIMARY KEY,
                message_type VARCHAR(20),
                message_content TEXT,
                target_count INTEGER,
                success_count INTEGER,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_by INTEGER REFERENCES users(id)
            )
        ''')
        
        # ユーザーにLINE user_idカラムを追加
        try:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS line_user_id VARCHAR(100)")
        except:
            pass
        
        # LINE設定の初期レコード
        cur.execute("SELECT COUNT(*) FROM line_settings")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO line_settings (is_enabled) VALUES (FALSE)")
        
        # 定期送信の初期設定（週次レポート、月次レポート、月謝利用料金）
        cur.execute("SELECT COUNT(*) FROM line_scheduled_messages WHERE report_type IS NOT NULL")
        if cur.fetchone()[0] == 0:
            # 週次レポート（毎週月曜 10:00）
            cur.execute("""
                INSERT INTO line_scheduled_messages (title, message_content, schedule_type, schedule_time, schedule_day, report_type, is_enabled)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, ('週次レポート', '【週次レポート】\n\n今週の実績をお知らせします。\n\n{weekly_report}\n\n引き続きよろしくお願いいたします。', 'weekly', '10:00', 1, 'weekly_report', False))
            
            # 月次レポート（毎月1日 10:30）
            cur.execute("""
                INSERT INTO line_scheduled_messages (title, message_content, schedule_type, schedule_time, schedule_day, report_type, is_enabled)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, ('月次レポート', '【月次レポート】\n\n先月の実績をお知らせします。\n\n{monthly_report}\n\n引き続きよろしくお願いいたします。', 'monthly', '10:30', 1, 'monthly_report', False))
            
            # 月謝利用料金の変更（毎月1日 11:00）
            cur.execute("""
                INSERT INTO line_scheduled_messages (title, message_content, schedule_type, schedule_time, schedule_day, report_type, is_enabled)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, ('月謝利用料金のお知らせ', '【月謝利用料金のお知らせ】\n\n今月のご利用料金をお知らせします。\n\n{monthly_fee}\n\nご確認よろしくお願いいたします。', 'monthly', '11:00', 1, 'monthly_fee', False))
        
        # ユーザー向け見積依頼書テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_mitsumori (
                id SERIAL PRIMARY KEY,
                document_no VARCHAR(50) NOT NULL,
                user_id INTEGER REFERENCES users(id),
                issue_date DATE NOT NULL,
                valid_until DATE,
                company_name VARCHAR(200),
                department VARCHAR(100),
                contact_person VARCHAR(100),
                address TEXT,
                subject VARCHAR(200),
                total_amount INTEGER DEFAULT 0,
                notes TEXT,
                status VARCHAR(20) DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # ユーザー向け見積依頼書明細テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_mitsumori_items (
                id SERIAL PRIMARY KEY,
                mitsumori_id INTEGER REFERENCES user_mitsumori(id) ON DELETE CASCADE,
                item_no INTEGER NOT NULL,
                item_name VARCHAR(200) NOT NULL,
                quantity INTEGER DEFAULT 1,
                unit VARCHAR(20),
                unit_price INTEGER DEFAULT 0,
                amount INTEGER DEFAULT 0
            )
        ''')
        
        # ユーザー向け計算書テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_keisan (
                id SERIAL PRIMARY KEY,
                document_no VARCHAR(50) NOT NULL,
                user_id INTEGER REFERENCES users(id),
                issue_date DATE NOT NULL,
                recipient_name VARCHAR(200),
                subject VARCHAR(200),
                total_amount INTEGER DEFAULT 0,
                notes TEXT,
                status VARCHAR(20) DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # ユーザー向け計算書明細テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_keisan_items (
                id SERIAL PRIMARY KEY,
                keisan_id INTEGER REFERENCES user_keisan(id) ON DELETE CASCADE,
                item_no INTEGER NOT NULL,
                item_name VARCHAR(200) NOT NULL,
                quantity INTEGER DEFAULT 1,
                unit VARCHAR(20),
                unit_price INTEGER DEFAULT 0,
                amount INTEGER DEFAULT 0
            )
        ''')
        
        # マスター: ブランドカテゴリ
        cur.execute('''
            CREATE TABLE IF NOT EXISTS master_brand_categories (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                display_order INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # マスター: ブランド名
        cur.execute('''
            CREATE TABLE IF NOT EXISTS master_brands (
                id SERIAL PRIMARY KEY,
                category_id INTEGER REFERENCES master_brand_categories(id),
                value VARCHAR(100) NOT NULL,
                display_name VARCHAR(200),
                keywords TEXT,
                display_order INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # マスター: 仕入先
        cur.execute('''
            CREATE TABLE IF NOT EXISTS master_suppliers (
                id SERIAL PRIMARY KEY,
                value VARCHAR(100) NOT NULL,
                display_name VARCHAR(200),
                display_order INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # マスター: 商品状態
        cur.execute('''
            CREATE TABLE IF NOT EXISTS master_conditions (
                id SERIAL PRIMARY KEY,
                value VARCHAR(20) NOT NULL,
                display_name VARCHAR(200),
                description TEXT,
                display_order INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # マスター: 支払方法
        cur.execute('''
            CREATE TABLE IF NOT EXISTS master_payment_methods (
                id SERIAL PRIMARY KEY,
                value VARCHAR(100) NOT NULL,
                display_name VARCHAR(200),
                display_order INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # マスター: 仕入先詳細
        cur.execute('''
            CREATE TABLE IF NOT EXISTS master_supplier_details (
                id SERIAL PRIMARY KEY,
                value VARCHAR(100) NOT NULL,
                display_name VARCHAR(200),
                display_order INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # マスター: 書類設定
        cur.execute('''
            CREATE TABLE IF NOT EXISTS master_document_settings (
                id SERIAL PRIMARY KEY,
                setting_key VARCHAR(100) UNIQUE NOT NULL,
                setting_value TEXT,
                setting_type VARCHAR(50) DEFAULT 'text',
                category VARCHAR(50) DEFAULT 'company',
                display_name VARCHAR(200),
                display_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 買取承諾書テーブル（ユーザー向け）
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_kaitori_shoudaku (
                id SERIAL PRIMARY KEY,
                document_no VARCHAR(50) NOT NULL,
                user_id INTEGER REFERENCES users(id),
                customer_name VARCHAR(100) NOT NULL,
                customer_address TEXT,
                customer_phone VARCHAR(50),
                issue_date DATE NOT NULL,
                subtotal INTEGER DEFAULT 0,
                tax_amount INTEGER DEFAULT 0,
                total_amount INTEGER DEFAULT 0,
                tax_rate DECIMAL(5,2) DEFAULT 0,
                payment_method VARCHAR(50),
                notes TEXT,
                status VARCHAR(20) DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 買取承諾書明細テーブル（ユーザー向け）
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_kaitori_shoudaku_items (
                id SERIAL PRIMARY KEY,
                kaitori_shoudaku_id INTEGER REFERENCES user_kaitori_shoudaku(id) ON DELETE CASCADE,
                item_no INTEGER NOT NULL,
                product_name VARCHAR(200) NOT NULL,
                brand_name VARCHAR(100),
                condition VARCHAR(50),
                quantity INTEGER DEFAULT 1,
                unit_price INTEGER DEFAULT 0,
                amount INTEGER DEFAULT 0,
                notes TEXT
            )
        ''')
        
        # 買取承諾書テーブル（法人版・管理者用）
        cur.execute('''
            CREATE TABLE IF NOT EXISTS admin_kaitori_shoudaku (
                id SERIAL PRIMARY KEY,
                document_no VARCHAR(50) NOT NULL,
                admin_id INTEGER REFERENCES users(id),
                company_name VARCHAR(200) NOT NULL,
                company_address TEXT,
                company_phone VARCHAR(50),
                contact_name VARCHAR(100),
                issue_date DATE NOT NULL,
                subtotal INTEGER DEFAULT 0,
                tax_amount INTEGER DEFAULT 0,
                total_amount INTEGER DEFAULT 0,
                tax_rate DECIMAL(5,2) DEFAULT 10.0,
                payment_method VARCHAR(50),
                bank_info TEXT,
                notes TEXT,
                status VARCHAR(20) DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 買取承諾書明細テーブル（法人版・管理者用）
        cur.execute('''
            CREATE TABLE IF NOT EXISTS admin_kaitori_shoudaku_items (
                id SERIAL PRIMARY KEY,
                kaitori_shoudaku_id INTEGER REFERENCES admin_kaitori_shoudaku(id) ON DELETE CASCADE,
                item_no INTEGER NOT NULL,
                product_name VARCHAR(200) NOT NULL,
                brand_name VARCHAR(100),
                condition VARCHAR(50),
                quantity INTEGER DEFAULT 1,
                unit_price INTEGER DEFAULT 0,
                amount INTEGER DEFAULT 0,
                notes TEXT
            )
        ''')
        
        # 問い合わせテーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS inquiries (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                category VARCHAR(50) DEFAULT 'general',
                title VARCHAR(200) NOT NULL,
                content TEXT NOT NULL,
                image_path TEXT,
                status VARCHAR(20) DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 既存テーブルにimage_pathカラムを追加
        try:
            cur.execute("ALTER TABLE inquiries ADD COLUMN IF NOT EXISTS image_path TEXT")
        except:
            pass
        
        # 問い合わせ返信テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS inquiry_replies (
                id SERIAL PRIMARY KEY,
                inquiry_id INTEGER REFERENCES inquiries(id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                is_admin_reply BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 売却申請テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS sale_requests (
                id SERIAL PRIMARY KEY,
                merchandise_id INTEGER REFERENCES merchandise(id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                sale_price INTEGER NOT NULL,
                qr_image_path TEXT,
                status VARCHAR(20) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                processed_by INTEGER REFERENCES users(id),
                admin_note TEXT
            )
        ''')
        
        # 販売代行申請テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS sales_agency_requests (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                service_type VARCHAR(50) NOT NULL,
                status VARCHAR(20) DEFAULT 'pending',
                admin_note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                processed_by INTEGER REFERENCES users(id),
                result_notified BOOLEAN DEFAULT FALSE
            )
        ''')
        
        # 販売代行申請商品テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS sales_agency_request_items (
                id SERIAL PRIMARY KEY,
                request_id INTEGER REFERENCES sales_agency_requests(id) ON DELETE CASCADE,
                merchandise_id INTEGER REFERENCES merchandise(id) ON DELETE CASCADE
            )
        ''')
        
        # デフォルト管理者作成
        cur.execute("SELECT * FROM users WHERE username = 'admin'")
        if not cur.fetchone():
            cur.execute('''
                INSERT INTO users (username, email, password_hash, role, display_name)
                VALUES (%s, %s, %s, %s, %s)
            ''', ('admin', 'admin@example.com', generate_password_hash('admin123'), 'admin', '管理者'))
        
        # autocommitモードなのでcommit()は不要だが、明示的にリソースを解放
        cur.close()
        conn.close()

else:
    import sqlite3
    
    DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'merchandise.db')
    
    def get_db():
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        return conn
    
    def init_db():
        conn = get_db()
        cur = conn.cursor()
        
        # ユーザーテーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                display_name TEXT,
                admin_permissions TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP
            )
        ''')
        
        # admin_permissionsカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE users ADD COLUMN admin_permissions TEXT")
        except:
            pass
        
        # Stripe関連カラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
        except:
            pass
        try:
            cur.execute("ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT")
        except:
            pass
        try:
            cur.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT DEFAULT 'inactive'")
        except:
            pass
        try:
            cur.execute("ALTER TABLE users ADD COLUMN last_payment_date TIMESTAMP")
        except:
            pass
        try:
            cur.execute("ALTER TABLE users ADD COLUMN next_payment_date TIMESTAMP")
        except:
            pass
        
        # 代行仕入れサービス利用可能金額カラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE users ADD COLUMN proxy_service_budget INTEGER DEFAULT 0")
        except:
            pass
        
        # 月謝免除フラグカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE users ADD COLUMN tuition_exempt INTEGER DEFAULT 0")
        except:
            pass
        
        # 未払い開始日カラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE users ADD COLUMN overdue_since TIMESTAMP")
        except:
            pass
        
        # オーナーがいない場合、最初の管理者をオーナーに昇格
        try:
            cur.execute("SELECT COUNT(*) FROM users WHERE role = 'owner'")
            owner_count = cur.fetchone()[0]
            if owner_count == 0:
                cur.execute("SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1")
                admin_row = cur.fetchone()
                if admin_row:
                    cur.execute("UPDATE users SET role = 'owner' WHERE id = ?", (admin_row[0],))
        except Exception as e:
            pass  # テーブルがまだ存在しない場合はスキップ
        
        # 商品テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS merchandise (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                purchase_date DATE,
                photo_path TEXT,
                product_name TEXT,
                brand_name TEXT,
                item_condition TEXT,
                store_name TEXT,
                purchase_price INTEGER DEFAULT 0,
                payment_method TEXT,
                listing_price INTEGER DEFAULT 0,
                expected_shipping INTEGER DEFAULT 0,
                expected_commission INTEGER DEFAULT 0,
                is_listed INTEGER DEFAULT 0,
                listing_date DATE,
                sale_date DATE,
                sale_type TEXT DEFAULT 'normal',
                sale_price INTEGER DEFAULT 0,
                shipping_cost INTEGER DEFAULT 0,
                sales_destination TEXT,
                commission INTEGER DEFAULT 0,
                is_shipped INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # brand_nameカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN brand_name TEXT")
        except:
            pass
        
        # item_conditionカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN item_condition TEXT")
        except:
            pass
        
        # additional_photosカラムを追加（複数画像対応）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN additional_photos TEXT")
        except:
            pass
        
        # sale_typeカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN sale_type TEXT DEFAULT 'normal'")
        except:
            pass
        
        # model_numberカラムを追加（型番）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN model_number TEXT")
        except:
            pass
        
        # kaika_product_codeカラムを追加（開花商品番号）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN kaika_product_code TEXT")
        except:
            pass
        
        # supplier_detailカラムを追加（仕入先詳細）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN supplier_detail TEXT")
        except:
            pass
        
        # id_document_pathカラムを追加（身分証）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN id_document_path TEXT")
        except:
            pass
        
        # consent_form_pathカラムを追加（同意書）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN consent_form_path TEXT")
        except:
            pass
        
        # updated_byカラムを追加（最終更新者）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN updated_by INTEGER REFERENCES users(id)")
        except:
            pass
        
        # updated_atカラムを追加（最終更新日時）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN updated_at TIMESTAMP")
        except:
            pass
        
        # notesカラムを追加（備考・メモ）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN notes TEXT")
        except:
            pass
        
        # 商品処分申請テーブル（merchandiseテーブルの後に作成）
        cur.execute('''
            CREATE TABLE IF NOT EXISTS item_disposal_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                merchandise_id INTEGER REFERENCES merchandise(id),
                disposal_type TEXT NOT NULL,
                reason TEXT DEFAULT 'overdue',
                shipping_address TEXT,
                shipping_name TEXT,
                shipping_phone TEXT,
                status TEXT DEFAULT 'pending',
                admin_note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                processed_by INTEGER REFERENCES users(id)
            )
        ''')
        
        # reasonカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE item_disposal_requests ADD COLUMN reason TEXT DEFAULT 'overdue'")
        except:
            pass
        
        # 顧客テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                address TEXT,
                total_purchase INTEGER DEFAULT 0,
                purchase_count INTEGER DEFAULT 0,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # お知らせテーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS announcements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                announcement_type TEXT DEFAULT 'info',
                is_active INTEGER DEFAULT 1,
                publish_at TIMESTAMP,
                expire_at TIMESTAMP,
                created_by INTEGER REFERENCES users(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # ウィジェット設定テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS widget_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                widget_key TEXT UNIQUE NOT NULL,
                widget_name TEXT NOT NULL,
                is_enabled INTEGER DEFAULT 1,
                display_order INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # デフォルトウィジェット設定を挿入
        default_widgets = [
            ('sales_profit', '売上・利益', 1, 1),
            ('top_products', '売れた商品', 1, 2),
            ('slow_products', '売れない商品', 1, 3),
            ('turnover_rate', '回転率・在庫日数', 1, 4),
            ('closing_rate', '成約率', 1, 5),
            ('avg_price', '平均単価', 1, 6),
            ('repeat_rate', 'リピート率', 0, 7),
            ('time_sales', '時間帯・曜日別売上', 0, 8),
            ('brand_stats', 'ブランド別統計', 1, 9),
            ('destination_stats', '販売先別統計', 1, 10),
        ]
        for widget in default_widgets:
            cur.execute('''
                INSERT OR IGNORE INTO widget_settings (widget_key, widget_name, is_enabled, display_order)
                VALUES (?, ?, ?, ?)
            ''', widget)
        
        # 精算書テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS shikiriosho (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_no TEXT NOT NULL,
                sender_id INTEGER REFERENCES users(id),
                recipient_id INTEGER REFERENCES users(id),
                recipient_name TEXT,
                issue_date DATE NOT NULL,
                due_date DATE,
                subtotal INTEGER DEFAULT 0,
                tax_amount INTEGER DEFAULT 0,
                total_amount INTEGER DEFAULT 0,
                tax_rate REAL DEFAULT 10.0,
                notes TEXT,
                status TEXT DEFAULT 'draft',
                is_read INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 精算書明細テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS shikiriosho_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shikiriosho_id INTEGER REFERENCES shikiriosho(id) ON DELETE CASCADE,
                item_no INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                specification TEXT,
                quantity INTEGER DEFAULT 1,
                unit_price INTEGER DEFAULT 0,
                amount INTEGER DEFAULT 0,
                notes TEXT
            )
        ''')
        
        # shikirioshoにcontact_name, personal_numberカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE shikiriosho ADD COLUMN contact_name TEXT")
        except:
            pass
        try:
            cur.execute("ALTER TABLE shikiriosho ADD COLUMN personal_number TEXT")
        except:
            pass
        
        # shikiriosho_itemsにproduct_date, product_codeカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE shikiriosho_items ADD COLUMN product_date TEXT")
        except:
            pass
        try:
            cur.execute("ALTER TABLE shikiriosho_items ADD COLUMN product_code TEXT")
        except:
            pass
        
        # 買取明細書テーブル（ユーザー→管理者）
        cur.execute('''
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_no TEXT NOT NULL,
                sender_id INTEGER REFERENCES users(id),
                issue_date DATE NOT NULL,
                payment_due_date DATE,
                recipient_name TEXT,
                postal_number TEXT,
                subtotal INTEGER DEFAULT 0,
                tax_amount_8 INTEGER DEFAULT 0,
                tax_amount_10 INTEGER DEFAULT 0,
                total_amount INTEGER DEFAULT 0,
                service_type TEXT DEFAULT 'normal',
                commission_rate REAL DEFAULT 10.00,
                commission_amount INTEGER DEFAULT 0,
                bank_info TEXT,
                notes TEXT,
                status TEXT DEFAULT 'draft',
                is_read INTEGER DEFAULT 0,
                approved_at TIMESTAMP,
                approved_by INTEGER REFERENCES users(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # service_type, commission_rate, commission_amountカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE invoices ADD COLUMN service_type TEXT DEFAULT 'normal'")
        except:
            pass
        try:
            cur.execute("ALTER TABLE invoices ADD COLUMN commission_rate REAL DEFAULT 10.00")
        except:
            pass
        try:
            cur.execute("ALTER TABLE invoices ADD COLUMN commission_amount INTEGER DEFAULT 0")
        except:
            pass
        try:
            cur.execute("ALTER TABLE invoices ADD COLUMN recipient_name TEXT")
        except:
            pass
        try:
            cur.execute("ALTER TABLE invoices ADD COLUMN postal_number TEXT")
        except:
            pass
        
        # 買取明細書明細テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS invoice_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER REFERENCES invoices(id) ON DELETE CASCADE,
                item_no INTEGER NOT NULL,
                tax_category TEXT DEFAULT '10',
                product_date DATE,
                product_name TEXT NOT NULL,
                quantity INTEGER DEFAULT 1,
                unit TEXT,
                unit_price INTEGER DEFAULT 0,
                amount INTEGER DEFAULT 0
            )
        ''')
        
        # invoice_itemsにproduct_codeカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE invoice_items ADD COLUMN product_code TEXT")
        except:
            pass
        
        # サービス書類テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS service_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_no TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id),
                service_type TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                contact TEXT,
                product_name TEXT NOT NULL,
                product_description TEXT,
                quantity INTEGER DEFAULT 1,
                unit_price INTEGER DEFAULT 0,
                commission INTEGER DEFAULT 0,
                total_amount INTEGER DEFAULT 0,
                service_data TEXT,
                notes TEXT,
                status TEXT DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 代行仕入れサービス設定テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS proxy_service_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                is_public INTEGER DEFAULT 0,
                page_title TEXT DEFAULT '代行仕入れサービス',
                page_description TEXT,
                start_datetime TIMESTAMP,
                end_datetime TIMESTAMP,
                updated_by INTEGER REFERENCES users(id),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 公開日時カラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE proxy_service_settings ADD COLUMN start_datetime TIMESTAMP")
        except:
            pass
        try:
            cur.execute("ALTER TABLE proxy_service_settings ADD COLUMN end_datetime TIMESTAMP")
        except:
            pass
        
        # 販売方式カラムを追加（既存テーブル用） auction=オークション, fixed=即決
        try:
            cur.execute("ALTER TABLE proxy_service_settings ADD COLUMN sale_mode TEXT DEFAULT 'auction'")
        except:
            pass
        
        # オークション名カラムを追加（複数オークション対応）
        try:
            cur.execute("ALTER TABLE proxy_service_settings ADD COLUMN auction_name TEXT DEFAULT 'オークション'")
        except:
            pass
        
        # 商品にオークションIDを追加（複数オークション対応）
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN auction_id INTEGER REFERENCES proxy_service_settings(id)")
        except:
            pass
        
        # 計算書に管理者作成フラグを追加（オークション落札用）
        try:
            cur.execute("ALTER TABLE user_keisan ADD COLUMN is_admin_created INTEGER DEFAULT 0")
        except:
            pass
        
        # 代行サービス公開ユーザーテーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS proxy_service_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                is_enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id)
            )
        ''')
        
        # 入札テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS proxy_service_bids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                merchandise_id INTEGER REFERENCES merchandise(id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES users(id),
                bidder_name TEXT NOT NULL,
                bid_amount INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # user_idカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE proxy_service_bids ADD COLUMN user_id INTEGER REFERENCES users(id)")
        except:
            pass
        
        # 商品に代行サービス表示フラグを追加
        try:
            cur.execute("ALTER TABLE merchandise ADD COLUMN show_in_proxy_service INTEGER DEFAULT 0")
        except:
            pass
        
        # デフォルト設定を挿入
        cur.execute("SELECT COUNT(*) FROM proxy_service_settings")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO proxy_service_settings (is_public) VALUES (0)")
        
        # LINE連携設定テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS line_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_access_token TEXT,
                channel_secret TEXT,
                is_enabled INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # LINE定期送信メッセージテーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS line_scheduled_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                message_content TEXT NOT NULL,
                schedule_type TEXT DEFAULT 'daily',
                schedule_time TEXT,
                schedule_day INTEGER,
                target_type TEXT DEFAULT 'all',
                report_type TEXT,
                is_enabled INTEGER DEFAULT 1,
                last_sent_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # report_typeカラムを追加（既存テーブル用）
        try:
            cur.execute("ALTER TABLE line_scheduled_messages ADD COLUMN report_type TEXT")
        except:
            pass
        
        # LINE送信履歴テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS line_message_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_type TEXT,
                message_content TEXT,
                target_count INTEGER,
                success_count INTEGER,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_by INTEGER REFERENCES users(id)
            )
        ''')
        
        # ユーザーにLINE user_idカラムを追加
        try:
            cur.execute("ALTER TABLE users ADD COLUMN line_user_id TEXT")
        except:
            pass
        
        # LINE設定の初期レコード
        cur.execute("SELECT COUNT(*) FROM line_settings")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO line_settings (is_enabled) VALUES (0)")
        
        # 定期送信の初期設定（週次レポート、月次レポート、月謝利用料金）
        cur.execute("SELECT COUNT(*) FROM line_scheduled_messages WHERE report_type IS NOT NULL")
        if cur.fetchone()[0] == 0:
            # 週次レポート（毎週月曜 10:00）
            cur.execute("""
                INSERT INTO line_scheduled_messages (title, message_content, schedule_type, schedule_time, schedule_day, report_type, is_enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, ('週次レポート', '【週次レポート】\n\n今週の実績をお知らせします。\n\n{weekly_report}\n\n引き続きよろしくお願いいたします。', 'weekly', '10:00', 1, 'weekly_report', 0))
            
            # 月次レポート（毎月1日 10:30）
            cur.execute("""
                INSERT INTO line_scheduled_messages (title, message_content, schedule_type, schedule_time, schedule_day, report_type, is_enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, ('月次レポート', '【月次レポート】\n\n先月の実績をお知らせします。\n\n{monthly_report}\n\n引き続きよろしくお願いいたします。', 'monthly', '10:30', 1, 'monthly_report', 0))
            
            # 月謝利用料金の変更（毎月1日 11:00）
            cur.execute("""
                INSERT INTO line_scheduled_messages (title, message_content, schedule_type, schedule_time, schedule_day, report_type, is_enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, ('月謝利用料金のお知らせ', '【月謝利用料金のお知らせ】\n\n今月のご利用料金をお知らせします。\n\n{monthly_fee}\n\nご確認よろしくお願いいたします。', 'monthly', '11:00', 1, 'monthly_fee', 0))
        
        # ユーザー向け見積依頼書テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_mitsumori (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_no TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id),
                issue_date DATE NOT NULL,
                valid_until DATE,
                company_name TEXT,
                department TEXT,
                contact_person TEXT,
                address TEXT,
                subject TEXT,
                total_amount INTEGER DEFAULT 0,
                notes TEXT,
                status TEXT DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # ユーザー向け見積依頼書明細テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_mitsumori_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mitsumori_id INTEGER REFERENCES user_mitsumori(id) ON DELETE CASCADE,
                item_no INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                quantity INTEGER DEFAULT 1,
                unit TEXT,
                unit_price INTEGER DEFAULT 0,
                amount INTEGER DEFAULT 0
            )
        ''')
        
        # ユーザー向け計算書テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_keisan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_no TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id),
                issue_date DATE NOT NULL,
                recipient_name TEXT,
                subject TEXT,
                total_amount INTEGER DEFAULT 0,
                notes TEXT,
                status TEXT DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # ユーザー向け計算書明細テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_keisan_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keisan_id INTEGER REFERENCES user_keisan(id) ON DELETE CASCADE,
                item_no INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                quantity INTEGER DEFAULT 1,
                unit TEXT,
                unit_price INTEGER DEFAULT 0,
                amount INTEGER DEFAULT 0
            )
        ''')
        
        # マスター: ブランドカテゴリ
        cur.execute('''
            CREATE TABLE IF NOT EXISTS master_brand_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                display_order INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # マスター: ブランド名
        cur.execute('''
            CREATE TABLE IF NOT EXISTS master_brands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER REFERENCES master_brand_categories(id),
                value TEXT NOT NULL,
                display_name TEXT,
                keywords TEXT,
                display_order INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # マスター: 仕入先
        cur.execute('''
            CREATE TABLE IF NOT EXISTS master_suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT NOT NULL,
                display_name TEXT,
                display_order INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # マスター: 商品状態
        cur.execute('''
            CREATE TABLE IF NOT EXISTS master_conditions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT NOT NULL,
                display_name TEXT,
                description TEXT,
                display_order INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # マスター: 支払方法
        cur.execute('''
            CREATE TABLE IF NOT EXISTS master_payment_methods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT NOT NULL,
                display_name TEXT,
                display_order INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # マスター: 仕入先詳細
        cur.execute('''
            CREATE TABLE IF NOT EXISTS master_supplier_details (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                value TEXT NOT NULL,
                display_name TEXT,
                display_order INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # マスター: 書類設定
        cur.execute('''
            CREATE TABLE IF NOT EXISTS master_document_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setting_key TEXT UNIQUE NOT NULL,
                setting_value TEXT,
                setting_type TEXT DEFAULT 'text',
                category TEXT DEFAULT 'company',
                display_name TEXT,
                display_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 買取承諾書テーブル（ユーザー向け）
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_kaitori_shoudaku (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_no TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id),
                customer_name TEXT NOT NULL,
                customer_address TEXT,
                customer_phone TEXT,
                issue_date DATE NOT NULL,
                subtotal INTEGER DEFAULT 0,
                tax_amount INTEGER DEFAULT 0,
                total_amount INTEGER DEFAULT 0,
                tax_rate REAL DEFAULT 0,
                payment_method TEXT,
                notes TEXT,
                status TEXT DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 買取承諾書明細テーブル（ユーザー向け）
        cur.execute('''
            CREATE TABLE IF NOT EXISTS user_kaitori_shoudaku_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kaitori_shoudaku_id INTEGER REFERENCES user_kaitori_shoudaku(id) ON DELETE CASCADE,
                item_no INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                brand_name TEXT,
                condition TEXT,
                quantity INTEGER DEFAULT 1,
                unit_price INTEGER DEFAULT 0,
                amount INTEGER DEFAULT 0,
                notes TEXT
            )
        ''')
        
        # 買取承諾書テーブル（法人版・管理者用）
        cur.execute('''
            CREATE TABLE IF NOT EXISTS admin_kaitori_shoudaku (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_no TEXT NOT NULL,
                admin_id INTEGER REFERENCES users(id),
                company_name TEXT NOT NULL,
                company_address TEXT,
                company_phone TEXT,
                contact_name TEXT,
                issue_date DATE NOT NULL,
                subtotal INTEGER DEFAULT 0,
                tax_amount INTEGER DEFAULT 0,
                total_amount INTEGER DEFAULT 0,
                tax_rate REAL DEFAULT 10.0,
                payment_method TEXT,
                bank_info TEXT,
                notes TEXT,
                status TEXT DEFAULT 'draft',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 買取承諾書明細テーブル（法人版・管理者用）
        cur.execute('''
            CREATE TABLE IF NOT EXISTS admin_kaitori_shoudaku_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kaitori_shoudaku_id INTEGER REFERENCES admin_kaitori_shoudaku(id) ON DELETE CASCADE,
                item_no INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                brand_name TEXT,
                condition TEXT,
                quantity INTEGER DEFAULT 1,
                unit_price INTEGER DEFAULT 0,
                amount INTEGER DEFAULT 0,
                notes TEXT
            )
        ''')
        
        # 問い合わせテーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS inquiries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                category TEXT DEFAULT 'general',
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                image_path TEXT,
                status TEXT DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 既存テーブルにimage_pathカラムを追加
        try:
            cur.execute("ALTER TABLE inquiries ADD COLUMN image_path TEXT")
        except:
            pass
        
        # 問い合わせ返信テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS inquiry_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER REFERENCES inquiries(id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL,
                is_admin_reply INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 売却申請テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS sale_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                merchandise_id INTEGER REFERENCES merchandise(id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                sale_price INTEGER NOT NULL,
                qr_image_path TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                processed_by INTEGER REFERENCES users(id),
                admin_note TEXT
            )
        ''')
        
        # 販売代行申請テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS sales_agency_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                service_type TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                admin_note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP,
                processed_by INTEGER REFERENCES users(id),
                result_notified INTEGER DEFAULT 0
            )
        ''')
        
        # 販売代行申請商品テーブル
        cur.execute('''
            CREATE TABLE IF NOT EXISTS sales_agency_request_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER REFERENCES sales_agency_requests(id) ON DELETE CASCADE,
                merchandise_id INTEGER REFERENCES merchandise(id) ON DELETE CASCADE
            )
        ''')
        
        # デフォルト管理者作成
        cur.execute("SELECT * FROM users WHERE username = 'admin'")
        if not cur.fetchone():
            cur.execute('''
                INSERT INTO users (username, email, password_hash, role, display_name)
                VALUES (?, ?, ?, ?, ?)
            ''', ('admin', 'admin@example.com', generate_password_hash('admin123'), 'admin', '管理者'))
        
        conn.commit()
        conn.close()

# ユーザークラス
class User(UserMixin):
    # 管理者が設定可能な権限一覧
    ADMIN_PERMISSION_OPTIONS = {
        'users': 'ユーザー管理',
        'shikiriosho': '精算書管理',
        'invoices': '買取明細書管理',
        'announcements': 'お知らせ管理',
        'analytics': '分析',
        'backup': 'バックアップ'
    }
    
    def __init__(self, id, username, email, role, display_name, admin_permissions=None, subscription_status=None, proxy_service_budget=0):
        self.id = id
        self.username = username
        self.email = email
        self.role = role
        self.display_name = display_name or username
        self.subscription_status = subscription_status or 'inactive'
        self.proxy_service_budget = proxy_service_budget or 0
        # admin_permissionsはJSON文字列またはリスト
        if admin_permissions:
            if isinstance(admin_permissions, str):
                try:
                    self.admin_permissions = json.loads(admin_permissions)
                except:
                    self.admin_permissions = []
            else:
                self.admin_permissions = admin_permissions
        else:
            self.admin_permissions = []
    
    def is_payment_overdue(self):
        """支払いが遅延しているか"""
        return self.subscription_status == 'past_due'
    
    def can_edit_merchandise(self):
        """商品の追加・編集が可能か（1ヶ月以上滞納で不可）"""
        # オーナーは常に可能
        if self.role == 'owner':
            return True
        # 支払い遅延中は編集不可（閲覧と商品処分のみ可能）
        return self.subscription_status != 'past_due'
    
    def can_participate_auction(self):
        """オークションに参加できるか"""
        # オーナーは常に参加可能、それ以外は支払い遅延時は不可
        if self.role == 'owner':
            return True
        return self.subscription_status != 'past_due'
    
    def is_owner(self):
        """オーナー権限を持っているか"""
        return self.role == 'owner'
    
    def is_admin(self):
        """管理者以上の権限を持っているか（オーナーも含む）"""
        return self.role in ['admin', 'owner']
    
    def has_permission(self, permission):
        """特定の権限を持っているか確認"""
        # オーナーは全権限を持つ
        if self.role == 'owner':
            return True
        # 管理者は設定された権限を持つ（空の場合は全権限）
        if self.role == 'admin':
            if not self.admin_permissions:
                return True  # 権限が設定されていない場合は全権限
            return permission in self.admin_permissions
        return False
    
    def get_proxy_service_used_amount(self):
        """代行仕入れサービスで使用済みの金額を取得"""
        conn = get_db()
        used_amount = 0
        
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # 即決購入で購入した商品の合計金額
            cur.execute("""
                SELECT COALESCE(SUM(sale_price), 0) as total
                FROM merchandise
                WHERE sales_destination LIKE %s AND sale_date IS NOT NULL
            """, (f'即決購入: {self.display_name}%',))
            result = cur.fetchone()
            used_amount = result['total'] if result else 0
        else:
            cur = conn.cursor()
            cur.execute("""
                SELECT COALESCE(SUM(sale_price), 0) as total
                FROM merchandise
                WHERE sales_destination LIKE ? AND sale_date IS NOT NULL
            """, (f'即決購入: {self.display_name}%',))
            result = cur.fetchone()
            used_amount = result[0] if result else 0
        
        cur.close()
        conn.close()
        return used_amount
    
    def get_proxy_service_remaining_budget(self):
        """代行仕入れサービスの残り利用可能金額を取得"""
        if self.proxy_service_budget == 0:
            return 0  # 0は0円（使えない）
        used = self.get_proxy_service_used_amount()
        return max(0, self.proxy_service_budget - used)
    
    def can_purchase_proxy_item(self, price):
        """代行仕入れサービスで指定金額の商品を購入できるか"""
        if self.proxy_service_budget == 0:
            return False  # 0は使えない
        remaining = self.get_proxy_service_remaining_budget()
        return remaining >= price
    
    def get_role_display(self):
        """権限の表示名を取得"""
        role_names = {
            'owner': 'オーナー',
            'admin': '管理者',
            'user': 'ユーザー'
        }
        return role_names.get(self.role, self.role)
    
    def get_permissions_display(self):
        """権限の表示用リストを取得"""
        if self.role == 'owner':
            return ['全権限']
        if not self.admin_permissions:
            return ['全権限']
        return [self.ADMIN_PERMISSION_OPTIONS.get(p, p) for p in self.admin_permissions]

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        user = cur.fetchone()
    cur.close()
    conn.close()
    
    if user:
        # admin_permissionsを安全に取得
        try:
            admin_permissions = user['admin_permissions']
        except (KeyError, TypeError):
            admin_permissions = None
        # subscription_statusを安全に取得
        try:
            subscription_status = user['subscription_status']
        except (KeyError, TypeError):
            subscription_status = 'inactive'
        # proxy_service_budgetを安全に取得
        try:
            proxy_service_budget = user['proxy_service_budget'] or 0
        except (KeyError, TypeError):
            proxy_service_budget = 0
        return User(user['id'], user['username'], user['email'], user['role'], user['display_name'], admin_permissions, subscription_status, proxy_service_budget)
    return None

# 管理者専用デコレータ
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin():
            flash('管理者権限が必要です', 'error')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# オーナー専用デコレータ
def owner_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_owner():
            flash('オーナー権限が必要です', 'error')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# 特定権限チェック用デコレータ
def permission_required(permission):
    """特定の管理者権限が必要なルートに使用するデコレータ"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                flash('ログインが必要です', 'error')
                return redirect(url_for('login'))
            if not current_user.has_permission(permission):
                flash(f'この機能へのアクセス権限がありません', 'error')
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# ヘルパー関数
def calculate_profit(sale_price, purchase_price, shipping_cost, commission):
    return sale_price - purchase_price - shipping_cost - commission

def calculate_profit_rate(profit, purchase_price):
    """利益率を計算（利益 ÷ 仕入れ金額 × 100）"""
    if purchase_price > 0:
        return round((profit / purchase_price) * 100, 1)
    return 0

def calculate_expected_profit(listing_price, purchase_price, expected_shipping, expected_commission):
    return listing_price - purchase_price - expected_shipping - expected_commission

def get_active_announcements():
    """アクティブなお知らせを取得"""
    conn = get_db()
    now = datetime.now()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT * FROM announcements 
            WHERE is_active = TRUE
            AND (publish_at IS NULL OR publish_at <= %s)
            AND (expire_at IS NULL OR expire_at > %s)
            ORDER BY created_at DESC
            LIMIT 5
        """, (now, now))
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM announcements 
            WHERE is_active = 1
            AND (publish_at IS NULL OR publish_at <= ?)
            AND (expire_at IS NULL OR expire_at > ?)
            ORDER BY created_at DESC
            LIMIT 5
        """, (now.strftime('%Y-%m-%d %H:%M:%S'), now.strftime('%Y-%m-%d %H:%M:%S')))
    
    announcements = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(a) for a in announcements]

# データベース初期化
init_db()

# デバッグ: どのDBを使用しているか表示
if DATABASE_URL:
    print("=" * 50)
    print("=== Using PostgreSQL ===")
    print(f"DATABASE_URL exists: {bool(DATABASE_URL)}")
    print("=" * 50)
else:
    print("=" * 50)
    print("=== Using SQLite ===")
    print("WARNING: DATABASE_URL is not set!")
    print("=" * 50)

# マスターテーブルにscopeカラムを追加するマイグレーション
def migrate_add_scope_column():
    """マスターテーブルにscopeカラムを追加（ユーザー機能/開花管理の分離用）"""
    try:
        conn = get_db()
        
        master_tables = [
            'master_brand_categories',
            'master_brands', 
            'master_suppliers',
            'master_conditions',
            'master_payment_methods',
            'master_supplier_details'
        ]
        
        if DATABASE_URL:
            cur = conn.cursor()
            for table in master_tables:
                try:
                    # カラムが存在しない場合のみ追加
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN scope VARCHAR(20) DEFAULT 'admin'")
                    conn.commit()
                    print(f"Added scope column to {table}")
                except Exception as e:
                    conn.rollback()
                    if 'already exists' in str(e).lower() or 'duplicate column' in str(e).lower():
                        pass  # カラムが既に存在する場合は無視
                    else:
                        print(f"Migration warning for {table}: {e}")
        else:
            cur = conn.cursor()
            for table in master_tables:
                try:
                    # SQLiteでカラムの存在確認
                    cur.execute(f"PRAGMA table_info({table})")
                    columns = [col[1] for col in cur.fetchall()]
                    if 'scope' not in columns:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN scope TEXT DEFAULT 'admin'")
                        conn.commit()
                        print(f"Added scope column to {table}")
                except Exception as e:
                    print(f"Migration warning for {table}: {e}")
        
        cur.close()
        conn.close()
        print("Scope column migration completed")
    except Exception as e:
        print(f"Migration error: {e}")

# マイグレーション実行
migrate_add_scope_column()

# ===================
# 認証関連ルート
# ===================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        # 管理者/オーナーの場合は管理者商品一覧へ
        if current_user.is_admin() or current_user.is_owner():
            return redirect(url_for('admin_items'))
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        print(f"[DEBUG] ログイン試行: username={username}")
        
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        else:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE username = ?", (username,))
        
        user = cur.fetchone()
        
        if user:
            print(f"[DEBUG] ユーザー発見: id={user['id']}, password_hash={user['password_hash'][:30]}...")
            password_check = check_password_hash(user['password_hash'], password)
            print(f"[DEBUG] パスワード検証結果: {password_check}")
        else:
            print(f"[DEBUG] ユーザーが見つかりません: {username}")
            password_check = False
        
        if user and password_check:
            # 3ヶ月以上滞納チェック（オーナー/管理者以外）
            if user['role'] not in ['owner', 'admin']:
                user_dict = dict(user) if DATABASE_URL else user
                subscription_status = user_dict.get('subscription_status') if DATABASE_URL else None
                overdue_since = user_dict.get('overdue_since') if DATABASE_URL else None
                
                # SQLiteの場合はカラム名でアクセス
                if not DATABASE_URL:
                    try:
                        subscription_status = user['subscription_status']
                        overdue_since = user['overdue_since']
                    except:
                        subscription_status = None
                        overdue_since = None
                
                if subscription_status == 'past_due' and overdue_since:
                    # 日付をパース
                    if isinstance(overdue_since, str):
                        try:
                            overdue_since = datetime.strptime(overdue_since, '%Y-%m-%d %H:%M:%S')
                        except:
                            try:
                                overdue_since = datetime.strptime(overdue_since, '%Y-%m-%d %H:%M:%S.%f')
                            except:
                                overdue_since = None
                    
                    if overdue_since:
                        overdue_days = (datetime.now() - overdue_since).days
                        if overdue_days >= 90:  # 3ヶ月 = 約90日
                            cur.close()
                            conn.close()
                            flash('月謝の未納が3ヶ月を超えているため、ログインできません。管理者にお問い合わせください。', 'error')
                            return render_template('login.html')
            
            # ログイン日時更新
            if DATABASE_URL:
                cur.execute("UPDATE users SET last_login = %s WHERE id = %s", 
                           (datetime.now(), user['id']))
            else:
                cur.execute("UPDATE users SET last_login = ? WHERE id = ?", 
                           (datetime.now(), user['id']))
            conn.commit()
            
            user_obj = User(user['id'], user['username'], user['email'], 
                          user['role'], user['display_name'])
            login_user(user_obj)
            
            flash(f'ようこそ、{user_obj.display_name}さん！', 'success')
            
            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)
            
            # 管理者/オーナーの場合は管理者商品一覧へ、一般ユーザーはダッシュボードへ
            if user['role'] in ['admin', 'owner']:
                return redirect(url_for('admin_items'))
            else:
                return redirect(url_for('index'))
        else:
            flash('ユーザー名またはパスワードが正しくありません', 'error')
        
        cur.close()
        conn.close()
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    """一般公開の登録は無効化 - 管理者のみがユーザーを作成可能"""
    flash('新規登録は管理者にお問い合わせください', 'info')
    return redirect(url_for('login'))

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('ログアウトしました', 'info')
    return redirect(url_for('login'))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'POST':
        display_name = request.form.get('display_name')
        email = request.form.get('email')
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM users WHERE id = %s", (current_user.id,))
        else:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE id = ?", (current_user.id,))
        
        user = cur.fetchone()
        
        # パスワード変更
        if current_password and new_password:
            if check_password_hash(user['password_hash'], current_password):
                if DATABASE_URL:
                    cur.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                               (generate_password_hash(new_password), current_user.id))
                else:
                    cur.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                               (generate_password_hash(new_password), current_user.id))
                flash('パスワードを変更しました', 'success')
            else:
                flash('現在のパスワードが正しくありません', 'error')
        
        # プロフィール更新
        if DATABASE_URL:
            cur.execute("UPDATE users SET display_name = %s, email = %s WHERE id = %s",
                       (display_name, email, current_user.id))
        else:
            cur.execute("UPDATE users SET display_name = ?, email = ? WHERE id = ?",
                       (display_name, email, current_user.id))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('プロフィールを更新しました', 'success')
        return redirect(url_for('profile'))
    
    # ユーザーの当月商品登録数と月額利用料を取得
    conn = get_db()
    if DATABASE_URL:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT u.subscription_status, u.stripe_subscription_id, 
                   u.last_payment_date, u.next_payment_date,
                   COUNT(CASE WHEN DATE_TRUNC('month', m.created_at) = DATE_TRUNC('month', CURRENT_DATE) THEN m.id END) as item_count,
                   COUNT(m.id) as total_item_count
            FROM users u
            LEFT JOIN merchandise m ON u.id = m.user_id
            WHERE u.id = %s
            GROUP BY u.id
        """, (current_user.id,))
        user_info = cur.fetchone()
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.subscription_status, u.stripe_subscription_id, 
                   u.last_payment_date, u.next_payment_date,
                   COUNT(CASE WHEN strftime('%%Y-%%m', m.created_at) = strftime('%%Y-%%m', 'now') THEN m.id END) as item_count,
                   COUNT(m.id) as total_item_count
            FROM users u
            LEFT JOIN merchandise m ON u.id = m.user_id
            WHERE u.id = ?
            GROUP BY u.id
        """, (current_user.id,))
        user_info = cur.fetchone()
        user_info = dict(user_info) if user_info else {}
    
    cur.close()
    conn.close()
    
    # 月額利用料を計算
    item_count = user_info.get('item_count', 0) if user_info else 0
    if item_count <= 20:
        monthly_fee = 2500
    elif item_count <= 50:
        monthly_fee = 5000
    elif item_count <= 100:
        monthly_fee = 10000
    elif item_count <= 200:
        monthly_fee = 20000
    else:
        monthly_fee = 30000  # 300件超は要相談
    
    total_item_count = user_info.get('total_item_count', 0) if user_info else 0
    
    billing_info = {
        'item_count': item_count,  # 当月の登録件数
        'total_item_count': total_item_count,  # 総登録件数
        'monthly_fee': monthly_fee,
        'subscription_status': user_info.get('subscription_status') if user_info else None,
        'has_subscription': bool(user_info.get('stripe_subscription_id')) if user_info else False,
        'last_payment_date': user_info.get('last_payment_date') if user_info else None,
        'next_payment_date': user_info.get('next_payment_date') if user_info else None
    }
    
    return render_template('profile.html', billing_info=billing_info)

# ===================
# 商品管理ルート
# ===================

@app.route('/')
@login_required
def index():
    # 管理者/オーナーの場合は管理者商品一覧へリダイレクト
    if current_user.is_admin() or current_user.is_owner():
        return redirect(url_for('admin_items'))
    
    filter_type = request.args.get('filter', '')
    search = request.args.get('search', '')
    
    conn = get_db()
    
    # オーナー/管理者の場合、全オーナー/管理者の商品を共有表示
    is_shared_view = current_user.is_admin() or current_user.is_owner()
    shared_user_ids = []
    shared_users = {}  # user_id -> display_name のマッピング
    
    # すべてのユーザーをマッピング（最終更新者表示用）
    all_users = {}
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, display_name, username FROM users")
    else:
        cur = conn.cursor()
        cur.execute("SELECT id, display_name, username FROM users")
    
    for u in cur.fetchall():
        u_dict = dict(u)
        all_users[u_dict['id']] = u_dict['display_name'] or u_dict['username']
    
    if is_shared_view:
        if DATABASE_URL:
            cur.execute("SELECT id, display_name, username FROM users WHERE role IN ('owner', 'admin')")
        else:
            cur.execute("SELECT id, display_name, username FROM users WHERE role IN ('owner', 'admin')")
        
        for u in cur.fetchall():
            u_dict = dict(u)
            shared_user_ids.append(u_dict['id'])
            shared_users[u_dict['id']] = u_dict['display_name'] or u_dict['username']
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if is_shared_view and shared_user_ids:
            placeholders = ','.join(['%s'] * len(shared_user_ids))
            query = f"SELECT * FROM merchandise WHERE user_id IN ({placeholders})"
            params = shared_user_ids.copy()
        else:
            query = "SELECT * FROM merchandise WHERE user_id = %s"
            params = [current_user.id]
    else:
        cur = conn.cursor()
        if is_shared_view and shared_user_ids:
            placeholders = ','.join(['?'] * len(shared_user_ids))
            query = f"SELECT * FROM merchandise WHERE user_id IN ({placeholders})"
            params = shared_user_ids.copy()
        else:
            query = "SELECT * FROM merchandise WHERE user_id = ?"
            params = [current_user.id]
    
    # フィルター
    today = datetime.now().date()
    if filter_type == 'today':
        if DATABASE_URL:
            query += " AND purchase_date = %s"
        else:
            query += " AND purchase_date = ?"
        params.append(str(today))
    elif filter_type == 'yesterday':
        yesterday = today - timedelta(days=1)
        if DATABASE_URL:
            query += " AND purchase_date = %s"
        else:
            query += " AND purchase_date = ?"
        params.append(str(yesterday))
    elif filter_type == 'week':
        week_ago = today - timedelta(days=7)
        if DATABASE_URL:
            query += " AND purchase_date >= %s"
        else:
            query += " AND purchase_date >= ?"
        params.append(str(week_ago))
    elif filter_type == 'month':
        month_ago = today - timedelta(days=30)
        if DATABASE_URL:
            query += " AND purchase_date >= %s"
        else:
            query += " AND purchase_date >= ?"
        params.append(str(month_ago))
    elif filter_type == 'sold':
        query += " AND sale_date IS NOT NULL"
    elif filter_type == 'unsold':
        query += " AND sale_date IS NULL"
    
    # 検索
    if search:
        if DATABASE_URL:
            query += " AND (product_name ILIKE %s OR store_name ILIKE %s)"
            params.extend([f'%{search}%', f'%{search}%'])
        else:
            query += " AND (product_name LIKE ? OR store_name LIKE ?)"
            params.extend([f'%{search}%', f'%{search}%'])
    
    query += " ORDER BY id DESC"
    cur.execute(query, params)
    items = cur.fetchall()
    
    # 統計
    if DATABASE_URL:
        if is_shared_view and shared_user_ids:
            placeholders = ','.join(['%s'] * len(shared_user_ids))
            cur.execute(f"""
                SELECT 
                    COUNT(*) as total_items,
                    COALESCE(SUM(purchase_price), 0) as total_purchase,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN sale_price ELSE 0 END), 0) as total_sales,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN 
                        sale_price - purchase_price - shipping_cost - commission ELSE 0 END), 0) as total_profit,
                    COUNT(CASE WHEN sale_date IS NOT NULL THEN 1 END) as sold_count,
                    COALESCE(SUM(CASE WHEN sale_date IS NULL THEN purchase_price ELSE 0 END), 0) as inventory_value
                FROM merchandise WHERE user_id IN ({placeholders})
            """, shared_user_ids)
        else:
            cur.execute("""
                SELECT 
                    COUNT(*) as total_items,
                    COALESCE(SUM(purchase_price), 0) as total_purchase,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN sale_price ELSE 0 END), 0) as total_sales,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN 
                        sale_price - purchase_price - shipping_cost - commission ELSE 0 END), 0) as total_profit,
                    COUNT(CASE WHEN sale_date IS NOT NULL THEN 1 END) as sold_count,
                    COALESCE(SUM(CASE WHEN sale_date IS NULL THEN purchase_price ELSE 0 END), 0) as inventory_value
                FROM merchandise WHERE user_id = %s
            """, (current_user.id,))
    else:
        if is_shared_view and shared_user_ids:
            placeholders = ','.join(['?'] * len(shared_user_ids))
            cur.execute(f"""
                SELECT 
                    COUNT(*) as total_items,
                    COALESCE(SUM(purchase_price), 0) as total_purchase,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN sale_price ELSE 0 END), 0) as total_sales,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN 
                        sale_price - purchase_price - shipping_cost - commission ELSE 0 END), 0) as total_profit,
                    COUNT(CASE WHEN sale_date IS NOT NULL THEN 1 END) as sold_count,
                    COALESCE(SUM(CASE WHEN sale_date IS NULL THEN purchase_price ELSE 0 END), 0) as inventory_value
                FROM merchandise WHERE user_id IN ({placeholders})
            """, shared_user_ids)
        else:
            cur.execute("""
                SELECT 
                    COUNT(*) as total_items,
                    COALESCE(SUM(purchase_price), 0) as total_purchase,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN sale_price ELSE 0 END), 0) as total_sales,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN 
                        sale_price - purchase_price - shipping_cost - commission ELSE 0 END), 0) as total_profit,
                    COUNT(CASE WHEN sale_date IS NOT NULL THEN 1 END) as sold_count,
                    COALESCE(SUM(CASE WHEN sale_date IS NULL THEN purchase_price ELSE 0 END), 0) as inventory_value
                FROM merchandise WHERE user_id = ?
            """, (current_user.id,))
    
    stats = cur.fetchone()
    cur.close()
    conn.close()
    
    # 申請中の売却申請を取得
    pending_requests = {}
    try:
        conn2 = get_db()
        if DATABASE_URL:
            cur2 = conn2.cursor(cursor_factory=RealDictCursor)
            cur2.execute("SELECT * FROM sale_requests WHERE status = 'pending'")
        else:
            cur2 = conn2.cursor()
            cur2.execute("SELECT * FROM sale_requests WHERE status = 'pending'")
        for req in cur2.fetchall():
            req_dict = dict(req)
            pending_requests[req_dict['merchandise_id']] = req_dict
        cur2.close()
        conn2.close()
    except Exception as e:
        print(f"Error fetching pending requests: {e}")
    
    # 販売代行サービス申請を取得（承認待ち・承認済みの両方）
    sales_agency_items = {}
    try:
        conn3 = get_db()
        if DATABASE_URL:
            cur3 = conn3.cursor(cursor_factory=RealDictCursor)
            cur3.execute("""
                SELECT sari.merchandise_id, sar.id as request_id, sar.service_type, sar.status, sar.created_at
                FROM sales_agency_request_items sari
                JOIN sales_agency_requests sar ON sari.request_id = sar.id
                WHERE sar.status IN ('pending', 'approved')
                  AND sar.service_type IN ('wholesale', 'auction')
            """)
        else:
            cur3 = conn3.cursor()
            cur3.execute("""
                SELECT sari.merchandise_id, sar.id as request_id, sar.service_type, sar.status, sar.created_at
                FROM sales_agency_request_items sari
                JOIN sales_agency_requests sar ON sari.request_id = sar.id
                WHERE sar.status IN ('pending', 'approved')
                  AND sar.service_type IN ('wholesale', 'auction')
            """)
        for req in cur3.fetchall():
            req_dict = dict(req)
            sales_agency_items[req_dict['merchandise_id']] = req_dict
        cur3.close()
        conn3.close()
    except Exception as e:
        print(f"Error fetching sales agency requests: {e}", flush=True)
    
    # アイテムに計算フィールド追加
    processed_items = []
    for item in items:
        item_dict = dict(item)
        if item_dict.get('photo_path'):
            item_dict['photo_path'] = item_dict['photo_path'].replace('\\', '/')
        
        # 共有ビューの場合、登録者名を追加
        if is_shared_view:
            item_dict['owner_name'] = shared_users.get(item_dict.get('user_id'), '不明')
        
        # 最終更新者名を追加
        updated_by_id = item_dict.get('updated_by')
        if updated_by_id:
            item_dict['updated_by_name'] = all_users.get(updated_by_id, '不明')
        else:
            item_dict['updated_by_name'] = '-'
        
        # 売却申請中かどうかを判定
        item_id = item_dict.get('id')
        if item_id in pending_requests:
            item_dict['pending_sale_request'] = True
            item_dict['sale_request'] = pending_requests[item_id]
        else:
            item_dict['pending_sale_request'] = False
            item_dict['sale_request'] = None
        
        # 販売代行サービス申請中かどうかを判定
        if item_id in sales_agency_items:
            item_dict['pending_sales_agency'] = True
            item_dict['sales_agency_request'] = sales_agency_items[item_id]
        else:
            item_dict['pending_sales_agency'] = False
            item_dict['sales_agency_request'] = None
        
        # 削除可能かどうかを判定（オーナーは常に削除可能、管理者は1日以内のみ削除可能）
        if current_user.is_owner():
            item_dict['can_delete'] = True
        else:
            created_at = item_dict.get('created_at')
            if created_at:
                if isinstance(created_at, str):
                    try:
                        created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                    except:
                        try:
                            created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S.%f')
                        except:
                            created_at = None
                if created_at:
                    item_dict['can_delete'] = datetime.now() - created_at <= timedelta(days=1)
                else:
                    item_dict['can_delete'] = True
            else:
                item_dict['can_delete'] = True
        
        if item_dict.get('sale_date'):
            item_dict['profit'] = calculate_profit(
                item_dict.get('sale_price', 0) or 0,
                item_dict.get('purchase_price', 0) or 0,
                item_dict.get('shipping_cost', 0) or 0,
                item_dict.get('commission', 0) or 0
            )
            item_dict['profit_rate'] = calculate_profit_rate(item_dict['profit'], item_dict.get('purchase_price', 0) or 0)
        else:
            item_dict['expected_profit'] = calculate_expected_profit(
                item_dict.get('listing_price', 0) or 0,
                item_dict.get('purchase_price', 0) or 0,
                item_dict.get('expected_shipping', 0) or 0,
                item_dict.get('expected_commission', 0) or 0
            )
        
        # 全画像リスト（メイン + 追加）
        item_dict['all_photos'] = []
        if item_dict.get('photo_path'):
            item_dict['all_photos'].append(item_dict['photo_path'])
        if item_dict.get('additional_photos'):
            try:
                additional_list = json.loads(item_dict['additional_photos'])
                item_dict['all_photos'].extend(additional_list)
            except:
                pass
        
        processed_items.append(item_dict)
    
    # アクティブなお知らせを取得
    announcements = get_active_announcements()
    
    # 代行仕入れサービスの利用可能残高を取得
    proxy_service_info = {
        'budget': current_user.proxy_service_budget or 0,
        'used': current_user.get_proxy_service_used_amount(),
        'remaining': current_user.get_proxy_service_remaining_budget()
    }
    
    return render_template('index.html', items=processed_items, stats=dict(stats),
                         filter_type=filter_type, search=search, announcements=announcements,
                         is_shared_view=is_shared_view, all_users=all_users,
                         proxy_service_info=proxy_service_info)

# ===================
# レポート機能
# ===================

@app.route('/reports')
@login_required
def reports():
    """レポートページ"""
    from datetime import datetime
    
    conn = get_db()
    current_year = datetime.now().year
    current_month = datetime.now().month
    years = list(range(current_year - 5, current_year + 1))
    
    is_admin_user = current_user.is_admin()
    
    # 在庫数と在庫総額を取得
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT COUNT(*) as count, COALESCE(SUM(purchase_price), 0) as total
            FROM merchandise 
            WHERE sale_date IS NULL
              AND ({user_filter})
        """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
        () if is_admin_user else (current_user.id,))
        result = cur.fetchone()

    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) as count, COALESCE(SUM(purchase_price), 0) as total
            FROM merchandise 
            WHERE sale_date IS NULL
              AND ({user_filter})
        """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
        () if is_admin_user else (current_user.id,))
        result = dict(cur.fetchone())

    
    inventory_count = result['count'] if result else 0
    inventory_total = result['total'] if result else 0
    
    cur.close()
    conn.close()
    
    return render_template('reports.html',
                          years=years,
                          current_year=current_year,
                          current_month=current_month,
                          inventory_count=inventory_count,
                          inventory_total=inventory_total)

@app.route('/api/report/<report_type>')
@login_required
def api_report(report_type):
    """レポートデータAPI"""
    from datetime import datetime
    import json
    
    year = request.args.get('year', datetime.now().year, type=int)
    month = request.args.get('month', datetime.now().month, type=int)
    
    # 管理者の場合は全データを表示
    is_admin_user = current_user.is_admin()
    
    conn = get_db()
    data = {'items': [], 'summary': {}}

    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        if report_type == 'monthly':
            # 月次売上報告書
            cur.execute("""
                SELECT id, sale_date, product_name, brand_name, store_name, sale_price, purchase_price, 
                       shipping_cost, commission, sale_type
                FROM merchandise 
                WHERE sale_date IS NOT NULL
                  AND ({user_filter})
                  AND EXTRACT(YEAR FROM sale_date) = %s
                  AND EXTRACT(MONTH FROM sale_date) = %s
                ORDER BY sale_date
            """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
            (year, month) if is_admin_user else (current_user.id, year, month))

            items = [dict(row) for row in cur.fetchall()]
            
            # 日付をシリアライズ可能に
            for item in items:
                if item.get('sale_date'):
                    item['sale_date'] = str(item['sale_date'])
            
            total_sales = int(sum(i['sale_price'] or 0 for i in items))
            total_purchase = int(sum(i['purchase_price'] or 0 for i in items))
            total_shipping = int(sum(i['shipping_cost'] or 0 for i in items))
            total_commission = int(sum(i['commission'] or 0 for i in items))
            total_profit = int(total_sales - total_purchase - total_shipping - total_commission)
            
            data = {
                'items': items,
                'summary': {
                    'count': len(items),
                    'total_sales': total_sales,
                    'total_purchase': total_purchase,
                    'total_shipping': total_shipping,
                    'total_commission': total_commission,
                    'total_profit': total_profit
                }
            }
            
        elif report_type == 'inventory':
            # 在庫一覧表
            cur.execute("""
                SELECT id, purchase_date, product_name, brand_name, item_condition,
                       purchase_price, listing_price, is_listed
                FROM merchandise 
                WHERE sale_date IS NULL
                  AND ({user_filter})
                ORDER BY purchase_date DESC
            """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
            () if is_admin_user else (current_user.id,))

            items = [dict(row) for row in cur.fetchall()]
            
            for item in items:
                if item.get('purchase_date'):
                    item['purchase_date'] = str(item['purchase_date'])
            
            total_purchase = int(sum(i['purchase_price'] or 0 for i in items))
            total_listing = int(sum(i['listing_price'] or 0 for i in items))
            
            data = {
                'items': items,
                'summary': {
                    'count': len(items),
                    'total_purchase': total_purchase,
                    'total_listing': total_listing
                }
            }
            
        elif report_type == 'expenses':
            # 月次経費精算書
            if month == 0:
                cur.execute("""
                    SELECT id, sale_date, product_name, sale_type, sale_price, 
                           shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND EXTRACT(YEAR FROM sale_date) = %s
                    ORDER BY sale_date
                """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
                (year,) if is_admin_user else (current_user.id, year))
            else:
                cur.execute("""
                    SELECT id, sale_date, product_name, sale_type, sale_price, 
                           shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND EXTRACT(YEAR FROM sale_date) = %s
                      AND EXTRACT(MONTH FROM sale_date) = %s
                    ORDER BY sale_date
                """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
                (year, month) if is_admin_user else (current_user.id, year, month))


            items = [dict(row) for row in cur.fetchall()]
            
            for item in items:
                if item.get('sale_date'):
                    item['sale_date'] = str(item['sale_date'])
            
            total_shipping = int(sum(i['shipping_cost'] or 0 for i in items))
            total_commission = int(sum(i['commission'] or 0 for i in items))
            
            data = {
                'items': items,
                'summary': {
                    'count': len(items),
                    'total_shipping': total_shipping,
                    'total_commission': total_commission,
                    'total_expenses': total_shipping + total_commission
                }
            }
            
        elif report_type == 'annual':
            # 年間収支報告書
            cur.execute("""
                SELECT EXTRACT(MONTH FROM sale_date) as month,
                       COUNT(*) as count,
                       COALESCE(SUM(sale_price), 0) as sales,
                       COALESCE(SUM(purchase_price), 0) as purchase,
                       COALESCE(SUM(shipping_cost), 0) as shipping,
                       COALESCE(SUM(commission), 0) as commission,
                       COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as profit
                FROM merchandise 
                WHERE sale_date IS NOT NULL
                  AND ({user_filter})
                  AND EXTRACT(YEAR FROM sale_date) = %s
                GROUP BY EXTRACT(MONTH FROM sale_date)
                ORDER BY month
            """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
            (year,) if is_admin_user else (current_user.id, year))

            monthly = [dict(row) for row in cur.fetchall()]
            
            # 各月のデータを整数に変換
            for m in monthly:
                m['month'] = int(m['month'])
                m['count'] = int(m['count'])
                m['sales'] = int(m['sales'])
                m['purchase'] = int(m['purchase'])
                m['shipping'] = int(m['shipping'])
                m['commission'] = int(m['commission'])
                m['profit'] = int(m['profit'])
            
            total_count = int(sum(m['count'] for m in monthly))
            total_sales = int(sum(m['sales'] for m in monthly))
            total_purchase = int(sum(m['purchase'] for m in monthly))
            total_shipping = int(sum(m['shipping'] for m in monthly))
            total_commission = int(sum(m['commission'] for m in monthly))
            total_profit = int(sum(m['profit'] for m in monthly))
            
            data = {
                'monthly': monthly,
                'summary': {
                    'count': total_count,
                    'total_sales': total_sales,
                    'total_purchase': total_purchase,
                    'total_expenses': total_shipping + total_commission,
                    'total_profit': total_profit
                }
            }
            
        elif report_type == 'kaitori':
            # 買取台帳（個人から仕入れた商品）
            if month == 0:
                cur.execute("""
                    SELECT id, purchase_date, product_name, brand_name, store_name,
                           purchase_price, id_document_path
                    FROM merchandise 
                    WHERE store_name = '個人'
                      AND ({user_filter})
                      AND EXTRACT(YEAR FROM purchase_date) = %s
                    ORDER BY purchase_date
                """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
                (year,) if is_admin_user else (current_user.id, year))
            else:
                cur.execute("""
                    SELECT id, purchase_date, product_name, brand_name, store_name,
                           purchase_price, id_document_path
                    FROM merchandise 
                    WHERE store_name = '個人'
                      AND ({user_filter})
                      AND EXTRACT(YEAR FROM purchase_date) = %s
                      AND EXTRACT(MONTH FROM purchase_date) = %s
                    ORDER BY purchase_date
                """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
                (year, month) if is_admin_user else (current_user.id, year, month))

            items = [dict(row) for row in cur.fetchall()]
            
            for item in items:
                if item.get('purchase_date'):
                    item['purchase_date'] = str(item['purchase_date'])
            
            total_purchase = int(sum(i['purchase_price'] or 0 for i in items))
            
            data = {
                'items': items,
                'summary': {
                    'count': len(items),
                    'total_purchase': total_purchase
                }
            }
            
        elif report_type == 'sales':
            # 売却一覧
            if month == 0:
                cur.execute("""
                    SELECT id, sale_date, product_name, brand_name, sales_destination,
                           sale_price, purchase_price, shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND EXTRACT(YEAR FROM sale_date) = %s
                    ORDER BY sale_date
                """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
                (year,) if is_admin_user else (current_user.id, year))
            else:
                cur.execute("""
                    SELECT id, sale_date, product_name, brand_name, sales_destination,
                           sale_price, purchase_price, shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND EXTRACT(YEAR FROM sale_date) = %s
                      AND EXTRACT(MONTH FROM sale_date) = %s
                    ORDER BY sale_date
                """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
                (year, month) if is_admin_user else (current_user.id, year, month))

            items = [dict(row) for row in cur.fetchall()]
            
            for item in items:
                if item.get('sale_date'):
                    item['sale_date'] = str(item['sale_date'])
            
            total_sales = int(sum(i['sale_price'] or 0 for i in items))
            total_profit = int(sum((i['sale_price'] or 0) - (i['purchase_price'] or 0) - (i['shipping_cost'] or 0) - (i['commission'] or 0) for i in items))
            
            data = {
                'items': items,
                'summary': {
                    'count': len(items),
                    'total_sales': total_sales,
                    'total_profit': total_profit
                }
            }
    else:
        # SQLite版
        cur = conn.cursor()
        
        if report_type == 'monthly':
            cur.execute("""
                SELECT id, sale_date, product_name, brand_name, store_name, sale_price, purchase_price, 
                       shipping_cost, commission, sale_type
                FROM merchandise 
                WHERE sale_date IS NOT NULL
                  AND ({user_filter})
                  AND strftime('%Y', sale_date) = ?
                  AND strftime('%m', sale_date) = ?
                ORDER BY sale_date
            """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
            (str(year), str(month).zfill(2)) if is_admin_user else (current_user.id, str(year), str(month).zfill(2)))

            items = [dict(row) for row in cur.fetchall()]
            
            total_sales = int(sum(i['sale_price'] or 0 for i in items))
            total_purchase = int(sum(i['purchase_price'] or 0 for i in items))
            total_shipping = int(sum(i['shipping_cost'] or 0 for i in items))
            total_commission = int(sum(i['commission'] or 0 for i in items))
            total_profit = int(total_sales - total_purchase - total_shipping - total_commission)
            
            data = {
                'items': items,
                'summary': {
                    'count': len(items),
                    'total_sales': total_sales,
                    'total_purchase': total_purchase,
                    'total_shipping': total_shipping,
                    'total_commission': total_commission,
                    'total_profit': total_profit
                }
            }
            
        elif report_type == 'inventory':
            cur.execute("""
                SELECT id, purchase_date, product_name, brand_name, item_condition,
                       purchase_price, listing_price, is_listed
                FROM merchandise 
                WHERE sale_date IS NULL
                  AND ({user_filter})
                ORDER BY purchase_date DESC
            """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
            () if is_admin_user else (current_user.id,))

            items = [dict(row) for row in cur.fetchall()]
            
            total_purchase = int(sum(i['purchase_price'] or 0 for i in items))
            total_listing = int(sum(i['listing_price'] or 0 for i in items))
            
            data = {
                'items': items,
                'summary': {
                    'count': len(items),
                    'total_purchase': total_purchase,
                    'total_listing': total_listing
                }
            }
            
        elif report_type == 'expenses':
            # 月次経費精算書
            if month == 0:
                cur.execute("""
                    SELECT id, sale_date, product_name, sale_type, sale_price, 
                           shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND strftime('%Y', sale_date) = ?
                    ORDER BY sale_date
                """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
                (str(year),) if is_admin_user else (current_user.id, str(year)))
            else:
                cur.execute("""
                    SELECT id, sale_date, product_name, sale_type, sale_price, 
                           shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND strftime('%Y', sale_date) = ?
                      AND strftime('%m', sale_date) = ?
                    ORDER BY sale_date
                """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
                (str(year), str(month).zfill(2)) if is_admin_user else (current_user.id, str(year), str(month).zfill(2)))


            items = [dict(row) for row in cur.fetchall()]
            
            total_shipping = int(sum(i['shipping_cost'] or 0 for i in items))
            total_commission = int(sum(i['commission'] or 0 for i in items))
            
            data = {
                'items': items,
                'summary': {
                    'count': len(items),
                    'total_shipping': total_shipping,
                    'total_commission': total_commission,
                    'total_expenses': total_shipping + total_commission
                }
            }
            
        elif report_type == 'annual':
            cur.execute("""
                SELECT CAST(strftime('%m', sale_date) AS INTEGER) as month,
                       COUNT(*) as count,
                       COALESCE(SUM(sale_price), 0) as sales,
                       COALESCE(SUM(purchase_price), 0) as purchase,
                       COALESCE(SUM(shipping_cost), 0) as shipping,
                       COALESCE(SUM(commission), 0) as commission,
                       COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as profit
                FROM merchandise 
                WHERE sale_date IS NOT NULL
                  AND ({user_filter})
                  AND strftime('%Y', sale_date) = ?
                GROUP BY strftime('%m', sale_date)
                ORDER BY month
            """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
            (str(year),) if is_admin_user else (current_user.id, str(year)))

            monthly = [dict(row) for row in cur.fetchall()]
            
            total_count = sum(m['count'] for m in monthly)
            total_sales = sum(m['sales'] for m in monthly)
            total_purchase = sum(m['purchase'] for m in monthly)
            total_shipping = sum(m['shipping'] for m in monthly)
            total_commission = sum(m['commission'] for m in monthly)
            total_profit = sum(m['profit'] for m in monthly)
            
            data = {
                'monthly': monthly,
                'summary': {
                    'count': total_count,
                    'total_sales': total_sales,
                    'total_purchase': total_purchase,
                    'total_expenses': total_shipping + total_commission,
                    'total_profit': total_profit
                }
            }
            
        elif report_type == 'kaitori':
            if month == 0:
                cur.execute("""
                    SELECT id, purchase_date, product_name, brand_name, store_name,
                           purchase_price, id_document_path
                    FROM merchandise 
                    WHERE store_name = '個人'
                      AND ({user_filter})
                      AND strftime('%Y', purchase_date) = ?
                    ORDER BY purchase_date
                """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
                (str(year),) if is_admin_user else (current_user.id, str(year)))
            else:
                cur.execute("""
                    SELECT id, purchase_date, product_name, brand_name, store_name,
                           purchase_price, id_document_path
                    FROM merchandise 
                    WHERE store_name = '個人'
                      AND ({user_filter})
                      AND strftime('%Y', purchase_date) = ?
                      AND strftime('%m', purchase_date) = ?
                    ORDER BY purchase_date
                """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
                (str(year), str(month).zfill(2)) if is_admin_user else (current_user.id, str(year), str(month).zfill(2)))

            items = [dict(row) for row in cur.fetchall()]
            
            total_purchase = int(sum(i['purchase_price'] or 0 for i in items))
            
            data = {
                'items': items,
                'summary': {
                    'count': len(items),
                    'total_purchase': total_purchase
                }
            }
            
        elif report_type == 'sales':
            if month == 0:
                cur.execute("""
                    SELECT id, sale_date, product_name, brand_name, sales_destination,
                           sale_price, purchase_price, shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND strftime('%Y', sale_date) = ?
                    ORDER BY sale_date
                """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
                (str(year),) if is_admin_user else (current_user.id, str(year)))
            else:
                cur.execute("""
                    SELECT id, sale_date, product_name, brand_name, sales_destination,
                           sale_price, purchase_price, shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND strftime('%Y', sale_date) = ?
                      AND strftime('%m', sale_date) = ?
                    ORDER BY sale_date
                """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
                (str(year), str(month).zfill(2)) if is_admin_user else (current_user.id, str(year), str(month).zfill(2)))

            items = [dict(row) for row in cur.fetchall()]
            
            total_sales = int(sum(i['sale_price'] or 0 for i in items))
            total_profit = int(sum((i['sale_price'] or 0) - (i['purchase_price'] or 0) - (i['shipping_cost'] or 0) - (i['commission'] or 0) for i in items))
            
            data = {
                'items': items,
                'summary': {
                    'count': len(items),
                    'total_sales': total_sales,
                    'total_profit': total_profit
                }
            }
    
    cur.close()
    conn.close()
    
    return jsonify(data)

@app.route('/api/report/<report_type>/download')
@login_required
def api_report_download(report_type):
    """レポートダウンロード"""
    from datetime import datetime
    import csv
    import io
    
    year = request.args.get('year', datetime.now().year, type=int)
    month = request.args.get('month', datetime.now().month, type=int)
    format_type = request.args.get('format', 'csv')
    
    # 管理者の場合は全データを表示
    is_admin_user = current_user.is_admin()
    
    # データ取得（api_reportと同様のロジック）

    conn = get_db()
    items = []
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        if report_type == 'monthly':
            cur.execute("""
                SELECT sale_date, product_name, brand_name, store_name, sale_price, purchase_price, 
                       shipping_cost, commission, sale_type
                FROM merchandise 
                WHERE sale_date IS NOT NULL
                  AND ({user_filter})
                  AND EXTRACT(YEAR FROM sale_date) = %s
                  AND EXTRACT(MONTH FROM sale_date) = %s
                ORDER BY sale_date
            """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
            (year, month) if is_admin_user else (current_user.id, year, month))

            items = [dict(row) for row in cur.fetchall()]
            filename = f"monthly_report_{year}_{month:02d}.csv"
            headers = ['売却日', '商品名', 'ブランド', '仕入先', '売上', '仕入', '送料', '手数料', '利益']
            
        elif report_type == 'inventory':
            cur.execute("""
                SELECT purchase_date, product_name, brand_name, item_condition,
                       purchase_price, listing_price, is_listed
                FROM merchandise 
                WHERE sale_date IS NULL
                  AND ({user_filter})
                ORDER BY purchase_date DESC
            """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
            () if is_admin_user else (current_user.id,))

            items = [dict(row) for row in cur.fetchall()]
            filename = f"inventory_{datetime.now().strftime('%Y%m%d')}.csv"
            headers = ['仕入日', '商品名', 'ブランド', '状態', '仕入額', '出品価格', 'ステータス']
            
        elif report_type == 'expenses':
            if month == 0:
                cur.execute("""
                    SELECT sale_date, product_name, sale_type, sale_price, 
                           shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND EXTRACT(YEAR FROM sale_date) = %s
                    ORDER BY sale_date
                """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
                (year,) if is_admin_user else (current_user.id, year))
                filename = f"expenses_{year}_all.csv"
            else:
                cur.execute("""
                    SELECT sale_date, product_name, sale_type, sale_price, 
                           shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND EXTRACT(YEAR FROM sale_date) = %s
                      AND EXTRACT(MONTH FROM sale_date) = %s
                    ORDER BY sale_date
                """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
                (year, month) if is_admin_user else (current_user.id, year, month))
                filename = f"expenses_{year}_{month:02d}.csv"

            items = [dict(row) for row in cur.fetchall()]
            headers = ['売却日', '商品名', '販売タイプ', '売上', '送料', '手数料', '経費計']

            
        elif report_type == 'annual':
            cur.execute("""
                SELECT sale_date, product_name, brand_name, sale_price, purchase_price, 
                       shipping_cost, commission
                FROM merchandise 
                WHERE sale_date IS NOT NULL
                  AND ({user_filter})
                  AND EXTRACT(YEAR FROM sale_date) = %s
                ORDER BY sale_date
            """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
            (year,) if is_admin_user else (current_user.id, year))

            items = [dict(row) for row in cur.fetchall()]
            filename = f"annual_report_{year}.csv"
            headers = ['売却日', '商品名', 'ブランド', '売上', '仕入', '送料', '手数料', '利益']
            
        elif report_type == 'kaitori':
            if month == 0:
                cur.execute("""
                    SELECT purchase_date, product_name, brand_name, store_name,
                           purchase_price
                    FROM merchandise 
                    WHERE store_name = '個人'
                      AND ({user_filter})
                      AND EXTRACT(YEAR FROM purchase_date) = %s
                    ORDER BY purchase_date
                """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
                (year,) if is_admin_user else (current_user.id, year))
            else:
                cur.execute("""
                    SELECT purchase_date, product_name, brand_name, store_name,
                           purchase_price
                    FROM merchandise 
                    WHERE store_name = '個人'
                      AND ({user_filter})
                      AND EXTRACT(YEAR FROM purchase_date) = %s
                      AND EXTRACT(MONTH FROM purchase_date) = %s
                    ORDER BY purchase_date
                """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
                (year, month) if is_admin_user else (current_user.id, year, month))

            items = [dict(row) for row in cur.fetchall()]
            filename = f"kaitori_ledger_{year}.csv" if month == 0 else f"kaitori_ledger_{year}_{month:02d}.csv"
            headers = ['仕入日', '商品名', 'ブランド', '仕入先', '買取金額']
            
        elif report_type == 'sales':
            if month == 0:
                cur.execute("""
                    SELECT sale_date, product_name, brand_name, sales_destination,
                           sale_price, purchase_price, shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND EXTRACT(YEAR FROM sale_date) = %s
                    ORDER BY sale_date
                """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
                (year,) if is_admin_user else (current_user.id, year))
            else:
                cur.execute("""
                    SELECT sale_date, product_name, brand_name, sales_destination,
                           sale_price, purchase_price, shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND EXTRACT(YEAR FROM sale_date) = %s
                      AND EXTRACT(MONTH FROM sale_date) = %s
                    ORDER BY sale_date
                """.format(user_filter='TRUE' if is_admin_user else 'user_id = %s'),
                (year, month) if is_admin_user else (current_user.id, year, month))

            items = [dict(row) for row in cur.fetchall()]
            filename = f"sales_list_{year}.csv" if month == 0 else f"sales_list_{year}_{month:02d}.csv"
            headers = ['売却日', '商品名', 'ブランド', '販売先', '売上', '仕入', '送料', '手数料', '利益']
    else:
        # SQLite版
        cur = conn.cursor()
        
        if report_type == 'monthly':
            cur.execute("""
                SELECT sale_date, product_name, brand_name, store_name, sale_price, purchase_price, 
                       shipping_cost, commission, sale_type
                FROM merchandise 
                WHERE sale_date IS NOT NULL
                  AND ({user_filter})
                  AND strftime('%Y', sale_date) = ?
                  AND strftime('%m', sale_date) = ?
                ORDER BY sale_date
            """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
            (str(year), str(month).zfill(2)) if is_admin_user else (current_user.id, str(year), str(month).zfill(2)))

            items = [dict(row) for row in cur.fetchall()]
            filename = f"monthly_report_{year}_{month:02d}.csv"
            headers = ['売却日', '商品名', 'ブランド', '仕入先', '売上', '仕入', '送料', '手数料', '利益']
            
        elif report_type == 'inventory':
            cur.execute("""
                SELECT purchase_date, product_name, brand_name, item_condition,
                       purchase_price, listing_price, is_listed
                FROM merchandise 
                WHERE sale_date IS NULL
                  AND ({user_filter})
                ORDER BY purchase_date DESC
            """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
            () if is_admin_user else (current_user.id,))

            items = [dict(row) for row in cur.fetchall()]
            filename = f"inventory_{datetime.now().strftime('%Y%m%d')}.csv"
            headers = ['仕入日', '商品名', 'ブランド', '状態', '仕入額', '出品価格', 'ステータス']
            
        elif report_type == 'expenses':
            if month == 0:
                cur.execute("""
                    SELECT sale_date, product_name, sale_type, sale_price, 
                           shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND strftime('%Y', sale_date) = ?
                    ORDER BY sale_date
                """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
                (str(year),) if is_admin_user else (current_user.id, str(year)))
                filename = f"expenses_{year}_all.csv"
            else:
                cur.execute("""
                    SELECT sale_date, product_name, sale_type, sale_price, 
                           shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND strftime('%Y', sale_date) = ?
                      AND strftime('%m', sale_date) = ?
                    ORDER BY sale_date
                """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
                (str(year), str(month).zfill(2)) if is_admin_user else (current_user.id, str(year), str(month).zfill(2)))
                filename = f"expenses_{year}_{month:02d}.csv"

            items = [dict(row) for row in cur.fetchall()]
            headers = ['売却日', '商品名', '販売タイプ', '売上', '送料', '手数料', '経費計']

            
        elif report_type == 'annual':
            cur.execute("""
                SELECT sale_date, product_name, brand_name, sale_price, purchase_price, 
                       shipping_cost, commission
                FROM merchandise 
                WHERE sale_date IS NOT NULL
                  AND ({user_filter})
                  AND strftime('%Y', sale_date) = ?
                ORDER BY sale_date
            """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
            (str(year),) if is_admin_user else (current_user.id, str(year)))

            items = [dict(row) for row in cur.fetchall()]
            filename = f"annual_report_{year}.csv"
            headers = ['売却日', '商品名', 'ブランド', '売上', '仕入', '送料', '手数料', '利益']
            
        elif report_type == 'kaitori':
            if month == 0:
                cur.execute("""
                    SELECT purchase_date, product_name, brand_name, store_name,
                           purchase_price
                    FROM merchandise 
                    WHERE store_name = '個人'
                      AND ({user_filter})
                      AND strftime('%Y', purchase_date) = ?
                    ORDER BY purchase_date
                """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
                (str(year),) if is_admin_user else (current_user.id, str(year)))
            else:
                cur.execute("""
                    SELECT purchase_date, product_name, brand_name, store_name,
                           purchase_price
                    FROM merchandise 
                    WHERE store_name = '個人'
                      AND ({user_filter})
                      AND strftime('%Y', purchase_date) = ?
                      AND strftime('%m', purchase_date) = ?
                    ORDER BY purchase_date
                """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
                (str(year), str(month).zfill(2)) if is_admin_user else (current_user.id, str(year), str(month).zfill(2)))

            items = [dict(row) for row in cur.fetchall()]
            filename = f"kaitori_ledger_{year}.csv" if month == 0 else f"kaitori_ledger_{year}_{month:02d}.csv"
            headers = ['仕入日', '商品名', 'ブランド', '仕入先', '買取金額']
            
        elif report_type == 'sales':
            if month == 0:
                cur.execute("""
                    SELECT sale_date, product_name, brand_name, sales_destination,
                           sale_price, purchase_price, shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND strftime('%Y', sale_date) = ?
                    ORDER BY sale_date
                """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
                (str(year),) if is_admin_user else (current_user.id, str(year)))
            else:
                cur.execute("""
                    SELECT sale_date, product_name, brand_name, sales_destination,
                           sale_price, purchase_price, shipping_cost, commission
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                      AND ({user_filter})
                      AND strftime('%Y', sale_date) = ?
                      AND strftime('%m', sale_date) = ?
                    ORDER BY sale_date
                """.format(user_filter='1=1' if is_admin_user else 'user_id = ?'),
                (str(year), str(month).zfill(2)) if is_admin_user else (current_user.id, str(year), str(month).zfill(2)))

            items = [dict(row) for row in cur.fetchall()]
            filename = f"sales_list_{year}.csv" if month == 0 else f"sales_list_{year}_{month:02d}.csv"
            headers = ['売却日', '商品名', 'ブランド', '販売先', '売上', '仕入', '送料', '手数料', '利益']
    
    cur.close()
    conn.close()
    
    # CSV生成
    if format_type == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        
        for item in items:
            if report_type == 'monthly':
                profit = (item.get('sale_price') or 0) - (item.get('purchase_price') or 0) - (item.get('shipping_cost') or 0) - (item.get('commission') or 0)
                writer.writerow([
                    item.get('sale_date'),
                    item.get('product_name'),
                    item.get('brand_name') or '',
                    item.get('store_name') or '',
                    item.get('sale_price') or 0,
                    item.get('purchase_price') or 0,
                    item.get('shipping_cost') or 0,
                    item.get('commission') or 0,
                    profit
                ])
            elif report_type == 'inventory':
                status = '出品中' if item.get('is_listed') else '未出品'
                writer.writerow([
                    item.get('purchase_date'),
                    item.get('product_name'),
                    item.get('brand_name') or '',
                    item.get('item_condition') or '',
                    item.get('purchase_price') or 0,
                    item.get('listing_price') or 0,
                    status
                ])
            elif report_type == 'expenses':
                expenses = (item.get('shipping_cost') or 0) + (item.get('commission') or 0)
                writer.writerow([
                    item.get('sale_date'),
                    item.get('product_name'),
                    item.get('sale_type') or '',
                    item.get('sale_price') or 0,
                    item.get('shipping_cost') or 0,
                    item.get('commission') or 0,
                    expenses
                ])
            elif report_type == 'annual':
                profit = (item.get('sale_price') or 0) - (item.get('purchase_price') or 0) - (item.get('shipping_cost') or 0) - (item.get('commission') or 0)
                writer.writerow([
                    item.get('sale_date'),
                    item.get('product_name'),
                    item.get('brand_name') or '',
                    item.get('sale_price') or 0,
                    item.get('purchase_price') or 0,
                    item.get('shipping_cost') or 0,
                    item.get('commission') or 0,
                    profit
                ])
            elif report_type == 'kaitori':
                writer.writerow([
                    item.get('purchase_date'),
                    item.get('product_name'),
                    item.get('brand_name') or '',
                    item.get('store_name') or '',
                    item.get('purchase_price') or 0
                ])
            elif report_type == 'sales':
                profit = (item.get('sale_price') or 0) - (item.get('purchase_price') or 0) - (item.get('shipping_cost') or 0) - (item.get('commission') or 0)
                writer.writerow([
                    item.get('sale_date'),
                    item.get('product_name'),
                    item.get('brand_name') or '',
                    item.get('sales_destination') or '',
                    item.get('sale_price') or 0,
                    item.get('purchase_price') or 0,
                    item.get('shipping_cost') or 0,
                    item.get('commission') or 0,
                    profit
                ])
        
        output.seek(0)
        
        # BOM付きUTF-8でエンコード（Excel対応）
        response = make_response('\ufeff' + output.getvalue())
        response.headers['Content-Type'] = 'text/csv; charset=utf-8'
        response.headers['Content-Disposition'] = f'attachment; filename={filename}'
        return response
    
    # PDF生成（簡易HTML版）
    elif format_type == 'pdf':
        flash('PDF機能は準備中です。CSVでダウンロードしてください。', 'info')
        return redirect(url_for('reports'))
    
    return redirect(url_for('reports'))

@app.route('/my-analytics')
@login_required
def user_analytics():
    """ユーザー向け分析ページ
    
    管理者ログイン時: 管理者以外のすべてのユーザーの商品を分析
    一般ユーザーログイン時: 自分自身の商品を分析
    """
    conn = get_db()
    analytics_data = {}
    
    # 期間フィルター取得
    start_month = request.args.get('start_month', '')
    end_month = request.args.get('end_month', '')
    
    # 管理者かどうかで条件を変更
    is_admin = current_user.is_admin()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 管理者の場合: user_id IS NOT NULL（管理者以外の全ユーザー）
        # 一般ユーザーの場合: user_id = current_user.id
        if is_admin:
            user_condition = "user_id IS NOT NULL"
            user_params = ()
        else:
            user_condition = "user_id = %s"
            user_params = (current_user.id,)
        
        # 期間フィルター条件（売上日ベース）
        sale_date_filter = ""
        purchase_date_filter = ""
        if start_month:
            sale_date_filter += f" AND sale_date >= '{start_month}-01'"
            purchase_date_filter += f" AND purchase_date >= '{start_month}-01'"
        if end_month:
            # 月の最終日を正しく計算
            try:
                year, month = map(int, end_month.split('-'))
                last_day = calendar.monthrange(year, month)[1]
                end_date = f"{end_month}-{last_day:02d}"
            except (ValueError, IndexError):
                # パースに失敗した場合は31日を使用（フォールバック）
                end_date = f"{end_month}-31"
            sale_date_filter += f" AND sale_date <= '{end_date}'"
            purchase_date_filter += f" AND purchase_date <= '{end_date}'"
        
        # 月別売上・利益推移
        cur.execute(f"""
            SELECT 
                TO_CHAR(sale_date, 'YYYY-MM') as month,
                COUNT(*) as count,
                COALESCE(SUM(sale_price), 0) as sales,
                COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as profit
            FROM merchandise 
            WHERE {user_condition} AND sale_date IS NOT NULL {sale_date_filter}
            GROUP BY TO_CHAR(sale_date, 'YYYY-MM')
            ORDER BY month DESC
            LIMIT 12
        """, user_params)
        analytics_data['monthly_sales'] = [dict(m) for m in cur.fetchall()]
        
        # 価格帯別統計
        cur.execute(f"""
            SELECT 
                CASE 
                    WHEN sale_price < 10000 THEN '1万円未満'
                    WHEN sale_price < 30000 THEN '1-3万円'
                    WHEN sale_price < 50000 THEN '3-5万円'
                    WHEN sale_price < 100000 THEN '5-10万円'
                    ELSE '10万円以上'
                END as price_range,
                COUNT(*) as count,
                COALESCE(SUM(sale_price), 0) as total_sales,
                COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as total_profit
            FROM merchandise 
            WHERE {user_condition} AND sale_date IS NOT NULL {sale_date_filter}
            GROUP BY price_range
            ORDER BY MIN(sale_price)
        """, user_params)
        analytics_data['price_stats'] = [dict(p) for p in cur.fetchall()]
        
        # ブランド別統計
        cur.execute(f"""
            SELECT 
                COALESCE(brand_name, '(ブランド名なし)') as brand_name,
                COUNT(*) as count,
                COALESCE(SUM(sale_price), 0) as total_sales,
                COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as total_profit,
                CASE WHEN SUM(purchase_price) > 0 
                    THEN ROUND(SUM(sale_price - purchase_price - shipping_cost - commission) * 100.0 / SUM(purchase_price), 1)
                    ELSE 0 END as profit_rate
            FROM merchandise 
            WHERE {user_condition} AND sale_date IS NOT NULL {sale_date_filter}
            GROUP BY brand_name
            ORDER BY total_profit DESC
            LIMIT 10
        """, user_params)
        analytics_data['brand_stats'] = [dict(b) for b in cur.fetchall()]
        
        # 販売タイプ別統計
        cur.execute(f"""
            SELECT 
                COALESCE(sale_type, 'normal') as sale_type,
                COUNT(*) as count,
                COALESCE(SUM(sale_price), 0) as total_sales,
                COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as total_profit
            FROM merchandise 
            WHERE {user_condition} AND sale_date IS NOT NULL {sale_date_filter}
            GROUP BY sale_type
            ORDER BY count DESC
        """, user_params)
        analytics_data['sale_type_stats'] = [dict(s) for s in cur.fetchall()]
        
        # 総合統計（期間フィルター適用）
        cur.execute(f"""
            SELECT 
                COUNT(*) as total_items,
                SUM(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN 1 ELSE 0 END) as sold_count,
                COALESCE(SUM(purchase_price), 0) as total_purchase,
                COALESCE(SUM(CASE WHEN sale_date IS NULL THEN purchase_price ELSE 0 END), 0) as inventory_value,
                COALESCE(SUM(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN sale_price ELSE 0 END), 0) as total_sales,
                COALESCE(SUM(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN sale_price - purchase_price - shipping_cost - commission ELSE 0 END), 0) as total_profit,
                COALESCE(AVG(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN sale_price ELSE NULL END), 0) as avg_sale_price,
                COALESCE(AVG(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN sale_price - purchase_price - shipping_cost - commission ELSE NULL END), 0) as avg_profit
            FROM merchandise 
            WHERE {user_condition}
        """, user_params)
        analytics_data['summary'] = dict(cur.fetchone() or {})
        
        # KPI用追加データ
        try:
            cur.execute(f"""
                SELECT 
                    COUNT(*) as total_items,
                    SUM(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN 1 ELSE 0 END) as sold_count,
                    SUM(CASE WHEN sale_date IS NULL THEN 1 ELSE 0 END) as unsold_count,
                    COALESCE(SUM(purchase_price), 0) as total_purchase,
                    COALESCE(SUM(CASE WHEN sale_date IS NULL THEN purchase_price ELSE 0 END), 0) as inventory_value,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN sale_price ELSE 0 END), 0) as total_sales,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN shipping_cost ELSE 0 END), 0) as total_shipping,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN commission ELSE 0 END), 0) as total_commission,
                    COALESCE(AVG(CASE WHEN sale_date IS NOT NULL {sale_date_filter} AND purchase_date IS NOT NULL THEN sale_date::date - purchase_date::date END), 0) as avg_days_to_sell
                FROM merchandise 
                WHERE {user_condition}
            """, user_params)
            analytics_data['kpi'] = dict(cur.fetchone() or {})
        except Exception as e:
            print(f"KPI query error: {e}")
            analytics_data['kpi'] = {'total_items': 0, 'sold_count': 0, 'unsold_count': 0, 'total_purchase': 0, 'inventory_value': 0, 'total_sales': 0, 'total_shipping': 0, 'total_commission': 0, 'avg_days_to_sell': 0}
        
        # 月別キャッシュフロー
        try:
            cur.execute(f"""
                SELECT 
                    TO_CHAR(COALESCE(sale_date, purchase_date), 'YYYY-MM') as month,
                    COALESCE(SUM(purchase_price), 0) as purchase_out,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN sale_price ELSE 0 END), 0) as sales_in,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN shipping_cost ELSE 0 END), 0) as shipping_out,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN commission ELSE 0 END), 0) as commission_out,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN shipping_cost + commission ELSE 0 END), 0) as expenses_out,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN sale_price - purchase_price - shipping_cost - commission ELSE 0 END), 0) as net_profit
                FROM merchandise 
                WHERE {user_condition} AND (purchase_date IS NOT NULL OR sale_date IS NOT NULL)
                GROUP BY TO_CHAR(COALESCE(sale_date, purchase_date), 'YYYY-MM')
                ORDER BY month DESC
                LIMIT 12
            """, user_params)
            analytics_data['cashflow'] = [dict(c) for c in cur.fetchall()]
        except Exception as e:
            print(f"Cashflow query error: {e}")
            analytics_data['cashflow'] = []
        
    else:
        import sqlite3
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        
        # 管理者の場合: user_id IS NOT NULL（管理者以外の全ユーザー）
        # 一般ユーザーの場合: user_id = current_user.id
        if is_admin:
            user_condition = "user_id IS NOT NULL"
            user_params = ()
        else:
            user_condition = "user_id = ?"
            user_params = (current_user.id,)
        
        # 期間フィルター条件（売上日ベース）
        sale_date_filter = ""
        purchase_date_filter = ""
        if start_month:
            sale_date_filter += f" AND sale_date >= '{start_month}-01'"
            purchase_date_filter += f" AND purchase_date >= '{start_month}-01'"
        if end_month:
            # 月の最終日を正しく計算
            try:
                year, month = map(int, end_month.split('-'))
                last_day = calendar.monthrange(year, month)[1]
                end_date = f"{end_month}-{last_day:02d}"
            except (ValueError, IndexError):
                # パースに失敗した場合は31日を使用（フォールバック）
                end_date = f"{end_month}-31"
            sale_date_filter += f" AND sale_date <= '{end_date}'"
            purchase_date_filter += f" AND purchase_date <= '{end_date}'"
        
        # 月別売上・利益推移
        cur.execute(f"""
            SELECT 
                strftime('%Y-%m', sale_date) as month,
                COUNT(*) as count,
                COALESCE(SUM(sale_price), 0) as sales,
                COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as profit
            FROM merchandise 
            WHERE {user_condition} AND sale_date IS NOT NULL {sale_date_filter}
            GROUP BY strftime('%Y-%m', sale_date)
            ORDER BY month DESC
            LIMIT 12
        """, user_params)
        analytics_data['monthly_sales'] = [dict(m) for m in cur.fetchall()]
        
        # 価格帯別統計
        cur.execute(f"""
            SELECT 
                CASE 
                    WHEN sale_price < 10000 THEN '1万円未満'
                    WHEN sale_price < 30000 THEN '1-3万円'
                    WHEN sale_price < 50000 THEN '3-5万円'
                    WHEN sale_price < 100000 THEN '5-10万円'
                    ELSE '10万円以上'
                END as price_range,
                COUNT(*) as count,
                COALESCE(SUM(sale_price), 0) as total_sales,
                COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as total_profit
            FROM merchandise 
            WHERE {user_condition} AND sale_date IS NOT NULL {sale_date_filter}
            GROUP BY price_range
            ORDER BY MIN(sale_price)
        """, user_params)
        analytics_data['price_stats'] = [dict(p) for p in cur.fetchall()]
        
        # ブランド別統計
        cur.execute(f"""
            SELECT 
                COALESCE(brand_name, '(ブランド名なし)') as brand_name,
                COUNT(*) as count,
                COALESCE(SUM(sale_price), 0) as total_sales,
                COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as total_profit,
                CASE WHEN SUM(purchase_price) > 0 
                    THEN ROUND(SUM(sale_price - purchase_price - shipping_cost - commission) * 100.0 / SUM(purchase_price), 1)
                    ELSE 0 END as profit_rate
            FROM merchandise 
            WHERE {user_condition} AND sale_date IS NOT NULL {sale_date_filter}
            GROUP BY brand_name
            ORDER BY total_profit DESC
            LIMIT 10
        """, user_params)
        analytics_data['brand_stats'] = [dict(b) for b in cur.fetchall()]
        
        # 販売タイプ別統計
        cur.execute(f"""
            SELECT 
                COALESCE(sale_type, 'normal') as sale_type,
                COUNT(*) as count,
                COALESCE(SUM(sale_price), 0) as total_sales,
                COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as total_profit
            FROM merchandise 
            WHERE {user_condition} AND sale_date IS NOT NULL {sale_date_filter}
            GROUP BY sale_type
            ORDER BY count DESC
        """, user_params)
        analytics_data['sale_type_stats'] = [dict(s) for s in cur.fetchall()]
        
        # 総合統計（期間フィルター適用）
        cur.execute(f"""
            SELECT 
                COUNT(*) as total_items,
                SUM(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN 1 ELSE 0 END) as sold_count,
                COALESCE(SUM(purchase_price), 0) as total_purchase,
                COALESCE(SUM(CASE WHEN sale_date IS NULL THEN purchase_price ELSE 0 END), 0) as inventory_value,
                COALESCE(SUM(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN sale_price ELSE 0 END), 0) as total_sales,
                COALESCE(SUM(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN sale_price - purchase_price - shipping_cost - commission ELSE 0 END), 0) as total_profit,
                COALESCE(AVG(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN sale_price ELSE NULL END), 0) as avg_sale_price,
                COALESCE(AVG(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN sale_price - purchase_price - shipping_cost - commission ELSE NULL END), 0) as avg_profit
            FROM merchandise 
            WHERE {user_condition}
        """, user_params)
        analytics_data['summary'] = dict(cur.fetchone() or {})
        
        # KPI用追加データ
        try:
            cur.execute(f"""
                SELECT 
                    COUNT(*) as total_items,
                    SUM(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN 1 ELSE 0 END) as sold_count,
                    SUM(CASE WHEN sale_date IS NULL THEN 1 ELSE 0 END) as unsold_count,
                    COALESCE(SUM(purchase_price), 0) as total_purchase,
                    COALESCE(SUM(CASE WHEN sale_date IS NULL THEN purchase_price ELSE 0 END), 0) as inventory_value,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN sale_price ELSE 0 END), 0) as total_sales,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN shipping_cost ELSE 0 END), 0) as total_shipping,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL {sale_date_filter} THEN commission ELSE 0 END), 0) as total_commission,
                    COALESCE(AVG(CASE WHEN sale_date IS NOT NULL {sale_date_filter} AND purchase_date IS NOT NULL THEN julianday(sale_date) - julianday(purchase_date) END), 0) as avg_days_to_sell
                FROM merchandise 
                WHERE {user_condition}
            """, user_params)
            analytics_data['kpi'] = dict(cur.fetchone() or {})
        except Exception as e:
            print(f"KPI query error: {e}")
            analytics_data['kpi'] = {'total_items': 0, 'sold_count': 0, 'unsold_count': 0, 'total_purchase': 0, 'inventory_value': 0, 'total_sales': 0, 'total_shipping': 0, 'total_commission': 0, 'avg_days_to_sell': 0}
        
        # 月別キャッシュフロー
        try:
            cur.execute(f"""
                SELECT 
                    strftime('%Y-%m', COALESCE(sale_date, purchase_date)) as month,
                    COALESCE(SUM(purchase_price), 0) as purchase_out,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN sale_price ELSE 0 END), 0) as sales_in,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN shipping_cost ELSE 0 END), 0) as shipping_out,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN commission ELSE 0 END), 0) as commission_out,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN shipping_cost + commission ELSE 0 END), 0) as expenses_out,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN sale_price - purchase_price - shipping_cost - commission ELSE 0 END), 0) as net_profit
                FROM merchandise 
                WHERE {user_condition} AND (purchase_date IS NOT NULL OR sale_date IS NOT NULL)
                GROUP BY strftime('%Y-%m', COALESCE(sale_date, purchase_date))
                ORDER BY month DESC
                LIMIT 12
            """, user_params)
            analytics_data['cashflow'] = [dict(c) for c in cur.fetchall()]
        except Exception as e:
            print(f"Cashflow query error: {e}")
            analytics_data['cashflow'] = []
    
    cur.close()
    conn.close()
    
    # 管理者の場合は分析対象がわかるようにフラグを渡す
    analytics_data['is_admin_view'] = is_admin
    
    return render_template('user_analytics.html', analytics=analytics_data,
                         start_month=start_month, end_month=end_month)

@app.route('/add', methods=['GET', 'POST'])
@login_required
def add_item():
    # 管理者のみ商品登録可能
    if not current_user.is_admin():
        flash('商品登録は管理者のみ可能です', 'error')
        return redirect(url_for('index'))
    
    # 滞納中は商品追加不可
    if not current_user.can_edit_merchandise():
        flash('月謝のお支払いが確認できていないため、商品の追加はできません。', 'error')
        return redirect(url_for('disposal_options'))
    
    if request.method == 'POST':
        photo_path = None
        additional_photos = []
        
        # メイン写真（1枚目）
        # Googleドライブからの画像を優先
        google_drive_photo_path = request.form.get('google_drive_photo_path')
        if google_drive_photo_path:
            photo_path = google_drive_photo_path
        elif 'photo' in request.files:
            file = request.files['photo']
            if file and file.filename and allowed_file(file.filename):
                filename = datetime.now().strftime('%Y%m%d_%H%M%S_') + secure_filename(file.filename)
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                photo_path = f'uploads/{filename}'
        
        # 追加写真（2-20枚目）
        # Googleドライブからの追加画像
        google_drive_additional = request.form.get('google_drive_additional_paths')
        if google_drive_additional:
            try:
                google_photos = json.loads(google_drive_additional)
                additional_photos.extend(google_photos)
                print(f"[DEBUG] Google Drive追加画像: {len(google_photos)}枚")
            except json.JSONDecodeError:
                pass
        
        print(f"[DEBUG] request.files keys: {list(request.files.keys())}")
        print(f"[DEBUG] 'additional_photos' in request.files: {'additional_photos' in request.files}")
        
        if 'additional_photos' in request.files:
            files = request.files.getlist('additional_photos')
            print(f"[DEBUG] 追加画像ファイル数: {len(files)}")
            for i, file in enumerate(files[:19]):  # 最大19枚まで（合計20枚）
                print(f"[DEBUG] ファイル{i}: filename={file.filename}, content_length={file.content_length if hasattr(file, 'content_length') else 'N/A'}")
                if file and file.filename and allowed_file(file.filename):
                    filename = datetime.now().strftime('%Y%m%d_%H%M%S_') + f'_{i+2}_' + secure_filename(file.filename)
                    file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                    additional_photos.append(f'uploads/{filename}')
                    print(f"[DEBUG] 追加画像保存: {filename}")
        
        print(f"[DEBUG] 追加画像合計: {len(additional_photos)}枚")
        additional_photos_json = json.dumps(additional_photos) if additional_photos else None
        print(f"[DEBUG] additional_photos_json: {additional_photos_json}")
        
        # 身分証ファイル
        id_document_path = None
        if 'id_document' in request.files:
            file = request.files['id_document']
            if file and file.filename:
                filename = datetime.now().strftime('%Y%m%d_%H%M%S_id_') + secure_filename(file.filename)
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                id_document_path = f'uploads/{filename}'
        
        # 同意書ファイル
        consent_form_path = None
        if 'consent_form' in request.files:
            file = request.files['consent_form']
            if file and file.filename:
                filename = datetime.now().strftime('%Y%m%d_%H%M%S_consent_') + secure_filename(file.filename)
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                consent_form_path = f'uploads/{filename}'
        
        # ステータスを取得（未出品/出品中/売却済み）
        item_status = request.form.get('item_status', 'unlisted')
        
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO merchandise (user_id, purchase_date, photo_path, additional_photos, product_name, kaika_product_code, brand_name, model_number, item_condition, store_name, 
                    supplier_detail, id_document_path, consent_form_path,
                    purchase_price, payment_method, listing_price, expected_shipping, expected_commission,
                    is_listed, listing_date, sale_date, sale_type, sale_price, shipping_cost, 
                    sales_destination, commission, is_shipped)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                current_user.id,
                request.form.get('purchase_date') or None,
                photo_path,
                additional_photos_json,
                request.form.get('product_name'),
                request.form.get('kaika_product_code'),
                request.form.get('brand_name'),
                request.form.get('model_number'),
                request.form.get('item_condition'),
                request.form.get('store_name'),
                request.form.get('supplier_detail'),
                id_document_path,
                consent_form_path,
                int(request.form.get('purchase_price') or 0),
                request.form.get('payment_method'),
                int(request.form.get('listing_price') or 0),
                int(request.form.get('expected_shipping') or 0),
                int(request.form.get('expected_commission') or 0),
                item_status in ['listed', 'sold'],  # is_listed: 出品中または売却済みならTrue
                request.form.get('listing_date') or None if item_status in ['listed', 'sold'] else None,
                request.form.get('sale_date') or None if item_status == 'sold' else None,
                request.form.get('sale_type') or 'normal',
                int(request.form.get('sale_price') or 0),
                int(request.form.get('shipping_cost') or 0),
                request.form.get('sales_destination'),
                int(request.form.get('commission') or 0),
                'is_shipped' in request.form
            ))
        else:
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO merchandise (user_id, purchase_date, photo_path, additional_photos, product_name, kaika_product_code, brand_name, model_number, item_condition, store_name, 
                    supplier_detail, id_document_path, consent_form_path,
                    purchase_price, payment_method, listing_price, expected_shipping, expected_commission,
                    is_listed, listing_date, sale_date, sale_type, sale_price, shipping_cost, 
                    sales_destination, commission, is_shipped)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                current_user.id,
                request.form.get('purchase_date') or None,
                photo_path,
                additional_photos_json,
                request.form.get('product_name'),
                request.form.get('kaika_product_code'),
                request.form.get('brand_name'),
                request.form.get('model_number'),
                request.form.get('item_condition'),
                request.form.get('store_name'),
                request.form.get('supplier_detail'),
                id_document_path,
                consent_form_path,
                int(request.form.get('purchase_price') or 0),
                request.form.get('payment_method'),
                int(request.form.get('listing_price') or 0),
                int(request.form.get('expected_shipping') or 0),
                int(request.form.get('expected_commission') or 0),
                1 if item_status in ['listed', 'sold'] else 0,  # is_listed: 出品中または売却済みなら1
                request.form.get('listing_date') or None if item_status in ['listed', 'sold'] else None,
                request.form.get('sale_date') or None if item_status == 'sold' else None,
                request.form.get('sale_type') or 'normal',
                int(request.form.get('sale_price') or 0),
                int(request.form.get('shipping_cost') or 0),
                request.form.get('sales_destination'),
                int(request.form.get('commission') or 0),
                1 if 'is_shipped' in request.form else 0
            ))
        
        conn.commit()
        conn.close()
        flash('商品を登録しました', 'success')
        return redirect(url_for('index'))
    
    return render_template('form.html', item=None)

@app.route('/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_item(id):
    # 管理者のみ商品編集可能
    if not current_user.is_admin():
        flash('商品編集は管理者のみ可能です', 'error')
        return redirect(url_for('index'))
    
    # 滞納中は商品編集不可
    if not current_user.can_edit_merchandise():
        flash('月謝のお支払いが確認できていないため、商品の編集はできません。', 'error')
        return redirect(url_for('disposal_options'))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # 管理者/オーナーは全商品編集可能、一般ユーザーは自分の商品のみ
        if current_user.is_admin() or current_user.is_owner():
            cur.execute("SELECT * FROM merchandise WHERE id = %s", (id,))
        else:
            cur.execute("SELECT * FROM merchandise WHERE id = %s AND user_id = %s", (id, current_user.id))
    else:
        cur = conn.cursor()
        # 管理者/オーナーは全商品編集可能、一般ユーザーは自分の商品のみ
        if current_user.is_admin() or current_user.is_owner():
            cur.execute("SELECT * FROM merchandise WHERE id = ?", (id,))
        else:
            cur.execute("SELECT * FROM merchandise WHERE id = ? AND user_id = ?", (id, current_user.id))
    
    item = cur.fetchone()
    if not item:
        flash('商品が見つかりません', 'error')
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        try:
            # オーナーかどうかを判定
            is_owner_user = current_user.is_owner()
            
            # sqlite3.Rowをdictに変換（.get()を使用可能にする）
            item_dict = dict(item)
            
            # 管理者（非オーナー）の場合、基本情報は元の値を維持
            if is_owner_user:
                # オーナーは全フィールド編集可能
                photo_path = item_dict['photo_path']
                
                # 既存の追加写真を取得
                existing_additional = item_dict.get('additional_photos')
                if existing_additional:
                    try:
                        additional_photos = json.loads(existing_additional) if isinstance(existing_additional, str) else existing_additional
                    except:
                        additional_photos = []
                else:
                    additional_photos = []
                
                # メイン写真の更新
                if 'photo' in request.files:
                    file = request.files['photo']
                    if file and file.filename and allowed_file(file.filename):
                        filename = datetime.now().strftime('%Y%m%d_%H%M%S_') + secure_filename(file.filename)
                        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                        photo_path = f'uploads/{filename}'
                
                # 追加写真の追加
                if 'additional_photos' in request.files:
                    files = request.files.getlist('additional_photos')
                    for i, file in enumerate(files):
                        if len(additional_photos) >= 19:  # 最大19枚まで
                            break
                        if file and file.filename and allowed_file(file.filename):
                            filename = datetime.now().strftime('%Y%m%d_%H%M%S_') + f'_{len(additional_photos)+2}_' + secure_filename(file.filename)
                            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                            additional_photos.append(f'uploads/{filename}')
                
                # 削除する写真を処理
                delete_photos = request.form.getlist('delete_photos')
                print(f"[DEBUG] delete_photos: {delete_photos}")
                print(f"[DEBUG] additional_photos before: {additional_photos}")
                if delete_photos:
                    additional_photos = [p for p in additional_photos if p not in delete_photos]
                print(f"[DEBUG] additional_photos after: {additional_photos}")
                
                additional_photos_json = json.dumps(additional_photos) if additional_photos else None
                
                # 身分証ファイル
                id_document_path = item_dict.get('id_document_path')
                if 'id_document' in request.files:
                    file = request.files['id_document']
                    if file and file.filename:
                        filename = datetime.now().strftime('%Y%m%d_%H%M%S_id_') + secure_filename(file.filename)
                        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                        id_document_path = f'uploads/{filename}'
                
                # 同意書ファイル
                consent_form_path = item_dict.get('consent_form_path')
                if 'consent_form' in request.files:
                    file = request.files['consent_form']
                    if file and file.filename:
                        filename = datetime.now().strftime('%Y%m%d_%H%M%S_consent_') + secure_filename(file.filename)
                        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                        consent_form_path = f'uploads/{filename}'
                
                # 基本情報はフォームから取得
                new_purchase_date = request.form.get('purchase_date') or None
                new_product_name = request.form.get('product_name')
                new_kaika_product_code = request.form.get('kaika_product_code')
                new_brand_name = request.form.get('brand_name')
                new_model_number = request.form.get('model_number')
                new_item_condition = request.form.get('item_condition')
                new_store_name = request.form.get('store_name')
                new_supplier_detail = request.form.get('supplier_detail')
                new_purchase_price = int(request.form.get('purchase_price') or 0)
                new_payment_method = request.form.get('payment_method')
            else:
                # 管理者は基本情報を元の値で維持（変更不可）、ただし画像は編集可能
                photo_path = item_dict['photo_path']
                
                # 既存の追加写真を取得
                existing_additional = item_dict.get('additional_photos')
                if existing_additional:
                    try:
                        additional_photos = json.loads(existing_additional) if isinstance(existing_additional, str) else existing_additional
                    except:
                        additional_photos = []
                else:
                    additional_photos = []
                
                # メイン写真の更新（管理者も可能）
                if 'photo' in request.files:
                    file = request.files['photo']
                    if file and file.filename and allowed_file(file.filename):
                        filename = datetime.now().strftime('%Y%m%d_%H%M%S_') + secure_filename(file.filename)
                        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                        photo_path = f'uploads/{filename}'
                
                # 追加写真の追加（管理者も可能）
                if 'additional_photos' in request.files:
                    files = request.files.getlist('additional_photos')
                    for i, file in enumerate(files):
                        if len(additional_photos) >= 19:  # 最大19枚まで
                            break
                        if file and file.filename and allowed_file(file.filename):
                            filename = datetime.now().strftime('%Y%m%d_%H%M%S_') + f'_{len(additional_photos)+2}_' + secure_filename(file.filename)
                            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                            additional_photos.append(f'uploads/{filename}')
                
                # 削除する写真を処理（管理者も可能）
                delete_photos = request.form.getlist('delete_photos')
                print(f"[DEBUG Admin] delete_photos: {delete_photos}")
                print(f"[DEBUG Admin] additional_photos before: {additional_photos}")
                if delete_photos:
                    additional_photos = [p for p in additional_photos if p not in delete_photos]
                print(f"[DEBUG Admin] additional_photos after: {additional_photos}")
                
                additional_photos_json = json.dumps(additional_photos) if additional_photos else None
                
                # 身分証・同意書は元の値を維持（変更不可）
                id_document_path = item_dict.get('id_document_path')
                consent_form_path = item_dict.get('consent_form_path')
                
                # 基本情報は元の値で維持（変更不可）
                new_purchase_date = item_dict['purchase_date']
                new_product_name = item_dict['product_name']
                new_kaika_product_code = item_dict.get('kaika_product_code')
                new_brand_name = item_dict['brand_name']
                new_model_number = item_dict.get('model_number')
                new_item_condition = item_dict['item_condition']
                new_store_name = item_dict['store_name']
                new_supplier_detail = item_dict.get('supplier_detail')
                new_purchase_price = item_dict['purchase_price']
                new_payment_method = item_dict['payment_method']
            
            # ステータスを取得（未出品/出品中/売却済み）
            item_status = request.form.get('item_status', 'unlisted')
            
            # 管理者/オーナーは全商品編集可能
            if DATABASE_URL:
                if current_user.is_admin() or current_user.is_owner():
                    cur.execute('''
                        UPDATE merchandise SET 
                            purchase_date = %s, photo_path = %s, additional_photos = %s, product_name = %s, kaika_product_code = %s, brand_name = %s, model_number = %s, item_condition = %s, store_name = %s,
                            supplier_detail = %s, id_document_path = %s, consent_form_path = %s,
                            purchase_price = %s, payment_method = %s, listing_price = %s, 
                            expected_shipping = %s, expected_commission = %s,
                            is_listed = %s, listing_date = %s, sale_date = %s, sale_type = %s, sale_price = %s,
                            shipping_cost = %s, sales_destination = %s, commission = %s, is_shipped = %s,
                            updated_by = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                    ''', (
                        new_purchase_date,
                        photo_path,
                        additional_photos_json,
                        new_product_name,
                        new_kaika_product_code,
                        new_brand_name,
                        new_model_number,
                        new_item_condition,
                        new_store_name,
                        new_supplier_detail,
                        id_document_path,
                        consent_form_path,
                        new_purchase_price,
                        new_payment_method,
                        int(request.form.get('listing_price') or 0),
                        int(request.form.get('expected_shipping') or 0),
                        int(request.form.get('expected_commission') or 0),
                        item_status in ['listed', 'sold'],  # is_listed: 出品中または売却済みならTrue
                        request.form.get('listing_date') or None if item_status in ['listed', 'sold'] else None,
                        request.form.get('sale_date') or None if item_status == 'sold' else None,
                        request.form.get('sale_type') or 'normal',
                        int(request.form.get('sale_price') or 0),
                        int(request.form.get('shipping_cost') or 0),
                        request.form.get('sales_destination'),
                        int(request.form.get('commission') or 0),
                        'is_shipped' in request.form,
                        current_user.id,
                        id
                    ))
                else:
                    cur.execute('''
                        UPDATE merchandise SET 
                            purchase_date = %s, photo_path = %s, additional_photos = %s, product_name = %s, kaika_product_code = %s, brand_name = %s, model_number = %s, item_condition = %s, store_name = %s,
                            supplier_detail = %s, id_document_path = %s, consent_form_path = %s,
                            purchase_price = %s, payment_method = %s, listing_price = %s, 
                            expected_shipping = %s, expected_commission = %s,
                            is_listed = %s, listing_date = %s, sale_date = %s, sale_type = %s, sale_price = %s,
                            shipping_cost = %s, sales_destination = %s, commission = %s, is_shipped = %s,
                            updated_by = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s AND user_id = %s
                    ''', (
                        new_purchase_date,
                        photo_path,
                        additional_photos_json,
                        new_product_name,
                        new_kaika_product_code,
                        new_brand_name,
                        new_model_number,
                        new_item_condition,
                        new_store_name,
                        new_supplier_detail,
                        id_document_path,
                        consent_form_path,
                        new_purchase_price,
                        new_payment_method,
                        int(request.form.get('listing_price') or 0),
                        int(request.form.get('expected_shipping') or 0),
                        int(request.form.get('expected_commission') or 0),
                        item_status in ['listed', 'sold'],  # is_listed: 出品中または売却済みならTrue
                        request.form.get('listing_date') or None if item_status in ['listed', 'sold'] else None,
                        request.form.get('sale_date') or None if item_status == 'sold' else None,
                        request.form.get('sale_type') or 'normal',
                        int(request.form.get('sale_price') or 0),
                        int(request.form.get('shipping_cost') or 0),
                        request.form.get('sales_destination'),
                        int(request.form.get('commission') or 0),
                        'is_shipped' in request.form,
                        current_user.id,
                        id, current_user.id
                    ))
            else:
                if current_user.is_admin() or current_user.is_owner():
                    cur.execute('''
                        UPDATE merchandise SET 
                            purchase_date = ?, photo_path = ?, additional_photos = ?, product_name = ?, kaika_product_code = ?, brand_name = ?, model_number = ?, item_condition = ?, store_name = ?,
                            supplier_detail = ?, id_document_path = ?, consent_form_path = ?,
                            purchase_price = ?, payment_method = ?, listing_price = ?, 
                            expected_shipping = ?, expected_commission = ?,
                            is_listed = ?, listing_date = ?, sale_date = ?, sale_type = ?, sale_price = ?,
                            shipping_cost = ?, sales_destination = ?, commission = ?, is_shipped = ?,
                            updated_by = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    ''', (
                        new_purchase_date,
                        photo_path,
                        additional_photos_json,
                        new_product_name,
                        new_kaika_product_code,
                        new_brand_name,
                        new_model_number,
                        new_item_condition,
                        new_store_name,
                        new_supplier_detail,
                        id_document_path,
                        consent_form_path,
                        new_purchase_price,
                        new_payment_method,
                        int(request.form.get('listing_price') or 0),
                        int(request.form.get('expected_shipping') or 0),
                        int(request.form.get('expected_commission') or 0),
                        1 if item_status in ['listed', 'sold'] else 0,  # is_listed: 出品中または売却済みなら1
                        request.form.get('listing_date') or None if item_status in ['listed', 'sold'] else None,
                        request.form.get('sale_date') or None if item_status == 'sold' else None,
                        request.form.get('sale_type') or 'normal',
                        int(request.form.get('sale_price') or 0),
                        int(request.form.get('shipping_cost') or 0),
                        request.form.get('sales_destination'),
                        int(request.form.get('commission') or 0),
                        1 if 'is_shipped' in request.form else 0,
                        current_user.id,
                        id
                    ))
                else:
                    cur.execute('''
                        UPDATE merchandise SET 
                            purchase_date = ?, photo_path = ?, additional_photos = ?, product_name = ?, kaika_product_code = ?, brand_name = ?, model_number = ?, item_condition = ?, store_name = ?,
                            supplier_detail = ?, id_document_path = ?, consent_form_path = ?,
                            purchase_price = ?, payment_method = ?, listing_price = ?, 
                            expected_shipping = ?, expected_commission = ?,
                            is_listed = ?, listing_date = ?, sale_date = ?, sale_type = ?, sale_price = ?,
                            shipping_cost = ?, sales_destination = ?, commission = ?, is_shipped = ?,
                            updated_by = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND user_id = ?
                    ''', (
                        new_purchase_date,
                        photo_path,
                        additional_photos_json,
                        new_product_name,
                        new_kaika_product_code,
                        new_brand_name,
                        new_model_number,
                        new_item_condition,
                        new_store_name,
                        new_supplier_detail,
                        id_document_path,
                        consent_form_path,
                        new_purchase_price,
                        new_payment_method,
                        int(request.form.get('listing_price') or 0),
                        int(request.form.get('expected_shipping') or 0),
                        int(request.form.get('expected_commission') or 0),
                        1 if item_status in ['listed', 'sold'] else 0,  # is_listed: 出品中または売却済みなら1
                        request.form.get('listing_date') or None if item_status in ['listed', 'sold'] else None,
                        request.form.get('sale_date') or None if item_status == 'sold' else None,
                        request.form.get('sale_type') or 'normal',
                        int(request.form.get('sale_price') or 0),
                        int(request.form.get('shipping_cost') or 0),
                        request.form.get('sales_destination'),
                        int(request.form.get('commission') or 0),
                        1 if 'is_shipped' in request.form else 0,
                        current_user.id,
                        id, current_user.id
                    ))
            
            conn.commit()
            cur.close()
            conn.close()
            flash('商品を更新しました', 'success')
            return redirect(url_for('index'))
        except Exception as e:
            print(f"[ERROR] edit_item: {e}")
            import traceback
            traceback.print_exc()
            flash(f'エラーが発生しました: {str(e)}', 'error')
            return redirect(url_for('edit_item', id=id))
    
    cur.close()
    conn.close()
    
    item_dict = dict(item)
    if item_dict.get('photo_path'):
        item_dict['photo_path'] = item_dict['photo_path'].replace('\\', '/')
    
    # 追加写真をパース
    if item_dict.get('additional_photos'):
        try:
            additional = json.loads(item_dict['additional_photos']) if isinstance(item_dict['additional_photos'], str) else item_dict['additional_photos']
            item_dict['additional_photos_list'] = [p.replace('\\', '/') for p in additional]
        except:
            item_dict['additional_photos_list'] = []
    else:
        item_dict['additional_photos_list'] = []
    
    return render_template('form.html', item=item_dict)

@app.route('/view/<int:id>')
@login_required
def view_item(id):
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # 管理者はすべての商品を閲覧可能
        if current_user.is_admin():
            cur.execute("SELECT * FROM merchandise WHERE id = %s", (id,))
        else:
            cur.execute("SELECT * FROM merchandise WHERE id = %s AND user_id = %s", (id, current_user.id))
    else:
        cur = conn.cursor()
        # 管理者はすべての商品を閲覧可能
        if current_user.is_admin():
            cur.execute("SELECT * FROM merchandise WHERE id = ?", (id,))
        else:
            cur.execute("SELECT * FROM merchandise WHERE id = ? AND user_id = ?", (id, current_user.id))
    
    item = cur.fetchone()
    cur.close()
    conn.close()
    
    if not item:
        flash('商品が見つかりません', 'error')
        return redirect(url_for('index'))
    
    item_dict = dict(item)
    if item_dict.get('photo_path'):
        item_dict['photo_path'] = item_dict['photo_path'].replace('\\', '/')
    
    # 追加写真をパース
    if item_dict.get('additional_photos'):
        try:
            additional = json.loads(item_dict['additional_photos']) if isinstance(item_dict['additional_photos'], str) else item_dict['additional_photos']
            item_dict['additional_photos_list'] = [p.replace('\\', '/') for p in additional]
        except:
            item_dict['additional_photos_list'] = []
    else:
        item_dict['additional_photos_list'] = []
    
    # 全画像リスト（メイン + 追加）
    item_dict['all_photos'] = []
    if item_dict.get('photo_path'):
        item_dict['all_photos'].append(item_dict['photo_path'])
    item_dict['all_photos'].extend(item_dict.get('additional_photos_list', []))
    
    if item_dict.get('sale_date'):
        item_dict['profit'] = calculate_profit(
            item_dict.get('sale_price', 0) or 0,
            item_dict.get('purchase_price', 0) or 0,
            item_dict.get('shipping_cost', 0) or 0,
            item_dict.get('commission', 0) or 0
        )
        item_dict['profit_rate'] = calculate_profit_rate(item_dict['profit'], item_dict.get('purchase_price', 0) or 0)
    else:
        item_dict['expected_profit'] = calculate_expected_profit(
            item_dict.get('listing_price', 0) or 0,
            item_dict.get('purchase_price', 0) or 0,
            item_dict.get('expected_shipping', 0) or 0,
            item_dict.get('expected_commission', 0) or 0
        )
    
    return render_template('view.html', item=item_dict)

@app.route('/delete/<int:id>')
@login_required
def delete_item(id):
    # 管理者のみ商品削除可能
    if not current_user.is_admin():
        flash('商品削除は管理者のみ可能です', 'error')
        return redirect(url_for('index'))
    
    # 滞納中は商品削除不可
    if not current_user.can_edit_merchandise():
        flash('月謝のお支払いが確認できていないため、商品の削除はできません。', 'error')
        return redirect(url_for('disposal_options'))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM merchandise WHERE id = %s AND user_id = %s", (id, current_user.id))
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM merchandise WHERE id = ? AND user_id = ?", (id, current_user.id))
    
    item = cur.fetchone()
    if not item:
        cur.close()
        conn.close()
        flash('商品が見つかりません', 'error')
        return redirect(url_for('index'))
    
    item_dict = dict(item)
    
    # 管理者（非オーナー）の場合、1日経った商品は削除不可
    if not current_user.is_owner():
        created_at = item_dict.get('created_at')
        if created_at:
            # created_atが文字列の場合はパース
            if isinstance(created_at, str):
                try:
                    created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                except:
                    try:
                        created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S.%f')
                    except:
                        created_at = None
            
            # 1日以上経過しているかチェック
            if created_at and datetime.now() - created_at > timedelta(days=1):
                cur.close()
                conn.close()
                flash('登録から1日以上経過した商品は削除できません。オーナーに連絡してください。', 'error')
                return redirect(url_for('index'))
    
    # 削除実行
    if DATABASE_URL:
        cur.execute("DELETE FROM merchandise WHERE id = %s AND user_id = %s", (id, current_user.id))
    else:
        cur.execute("DELETE FROM merchandise WHERE id = ? AND user_id = ?", (id, current_user.id))
    
    conn.commit()
    cur.close()
    conn.close()
    flash('商品を削除しました', 'info')
    return redirect(url_for('index'))

@app.route('/export_csv')
@login_required
def export_csv():
    conn = get_db()
    
    # オーナー/管理者の場合、全オーナー/管理者の商品を共有出力
    is_shared_view = current_user.is_admin() or current_user.is_owner()
    shared_user_ids = []
    shared_users = {}
    
    if is_shared_view:
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT id, display_name, username FROM users WHERE role IN ('owner', 'admin')")
        else:
            cur = conn.cursor()
            cur.execute("SELECT id, display_name, username FROM users WHERE role IN ('owner', 'admin')")
        
        for u in cur.fetchall():
            u_dict = dict(u)
            shared_user_ids.append(u_dict['id'])
            shared_users[u_dict['id']] = u_dict['display_name'] or u_dict['username']
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if is_shared_view and shared_user_ids:
            placeholders = ','.join(['%s'] * len(shared_user_ids))
            cur.execute(f"SELECT * FROM merchandise WHERE user_id IN ({placeholders}) ORDER BY id", shared_user_ids)
        else:
            cur.execute("SELECT * FROM merchandise WHERE user_id = %s ORDER BY id", (current_user.id,))
    else:
        cur = conn.cursor()
        if is_shared_view and shared_user_ids:
            placeholders = ','.join(['?'] * len(shared_user_ids))
            cur.execute(f"SELECT * FROM merchandise WHERE user_id IN ({placeholders}) ORDER BY id", shared_user_ids)
        else:
            cur.execute("SELECT * FROM merchandise WHERE user_id = ? ORDER BY id", (current_user.id,))
    
    items = cur.fetchall()
    cur.close()
    conn.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # 共有ビューの場合は登録者列を追加
    if is_shared_view:
        writer.writerow(['ID', '登録者', '仕入日', '商品名', '店舗名', '仕入額', '支払方法', 
                        '出品価格', '出品済み', '出品日', '売却日', '売上金', 
                        '送料', '販売先', '手数料', '利益', '利益率', '発送済み'])
    else:
        writer.writerow(['ID', '仕入日', '商品名', '店舗名', '仕入額', '支払方法', 
                        '出品価格', '出品済み', '出品日', '売却日', '売上金', 
                        '送料', '販売先', '手数料', '利益', '利益率', '発送済み'])
    
    for item in items:
        item_dict = dict(item)
        profit = calculate_profit(
            item_dict.get('sale_price', 0) or 0,
            item_dict.get('purchase_price', 0) or 0,
            item_dict.get('shipping_cost', 0) or 0,
            item_dict.get('commission', 0) or 0
        )
        profit_rate = calculate_profit_rate(profit, item_dict.get('purchase_price', 0) or 0)
        
        if is_shared_view:
            owner_name = shared_users.get(item_dict.get('user_id'), '不明')
            writer.writerow([
                item_dict['id'],
                owner_name,
                item_dict.get('purchase_date', ''),
                item_dict.get('product_name', ''),
                item_dict.get('store_name', ''),
                item_dict.get('purchase_price', 0),
                item_dict.get('payment_method', ''),
                item_dict.get('listing_price', 0),
                '済' if item_dict.get('is_listed') else '',
                item_dict.get('listing_date', ''),
                item_dict.get('sale_date', ''),
                item_dict.get('sale_price', 0),
                item_dict.get('shipping_cost', 0),
                item_dict.get('sales_destination', ''),
                item_dict.get('commission', 0),
                profit,
                f'{profit_rate}%',
                '済' if item_dict.get('is_shipped') else ''
            ])
        else:
            writer.writerow([
                item_dict['id'],
                item_dict.get('purchase_date', ''),
                item_dict.get('product_name', ''),
                item_dict.get('store_name', ''),
                item_dict.get('purchase_price', 0),
                item_dict.get('payment_method', ''),
                item_dict.get('listing_price', 0),
                '済' if item_dict.get('is_listed') else '',
                item_dict.get('listing_date', ''),
                item_dict.get('sale_date', ''),
                item_dict.get('sale_price', 0),
                item_dict.get('shipping_cost', 0),
                item_dict.get('sales_destination', ''),
                item_dict.get('commission', 0),
                profit,
                f'{profit_rate}%',
                '済' if item_dict.get('is_shipped') else ''
            ])
    
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode('utf-8-sig')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'merchandise_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
    )

# ===================
# 顧客管理ルート
# ===================

@app.route('/customers')
@login_required
def customers():
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM customers WHERE user_id = %s ORDER BY total_purchase DESC", (current_user.id,))
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM customers WHERE user_id = ? ORDER BY total_purchase DESC", (current_user.id,))
    
    customers_list = cur.fetchall()
    cur.close()
    conn.close()
    
    processed_customers = []
    for c in customers_list:
        c_dict = dict(c)
        processed_customers.append(c_dict)
    
    return render_template('customers.html', customers=processed_customers)

@app.route('/customers/add', methods=['GET', 'POST'])
@login_required
def add_customer():
    if request.method == 'POST':
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO customers (user_id, name, email, phone, address, total_purchase, purchase_count, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                current_user.id,
                request.form.get('name'),
                request.form.get('email'),
                request.form.get('phone'),
                request.form.get('address'),
                int(request.form.get('total_purchase') or 0),
                int(request.form.get('purchase_count') or 0),
                request.form.get('notes')
            ))
        else:
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO customers (user_id, name, email, phone, address, total_purchase, purchase_count, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                current_user.id,
                request.form.get('name'),
                request.form.get('email'),
                request.form.get('phone'),
                request.form.get('address'),
                int(request.form.get('total_purchase') or 0),
                int(request.form.get('purchase_count') or 0),
                request.form.get('notes')
            ))
        conn.commit()
        conn.close()
        flash('顧客を登録しました', 'success')
        return redirect(url_for('customers'))
    
    return render_template('customer_form.html', customer=None)

@app.route('/customers/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_customer(id):
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM customers WHERE id = %s AND user_id = %s", (id, current_user.id))
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM customers WHERE id = ? AND user_id = ?", (id, current_user.id))
    
    customer = cur.fetchone()
    if not customer:
        flash('顧客が見つかりません', 'error')
        return redirect(url_for('customers'))
    
    if request.method == 'POST':
        if DATABASE_URL:
            cur.execute('''
                UPDATE customers SET name = %s, email = %s, phone = %s, address = %s,
                    total_purchase = %s, purchase_count = %s, notes = %s
                WHERE id = %s AND user_id = %s
            ''', (
                request.form.get('name'),
                request.form.get('email'),
                request.form.get('phone'),
                request.form.get('address'),
                int(request.form.get('total_purchase') or 0),
                int(request.form.get('purchase_count') or 0),
                request.form.get('notes'),
                id, current_user.id
            ))
        else:
            cur.execute('''
                UPDATE customers SET name = ?, email = ?, phone = ?, address = ?,
                    total_purchase = ?, purchase_count = ?, notes = ?
                WHERE id = ? AND user_id = ?
            ''', (
                request.form.get('name'),
                request.form.get('email'),
                request.form.get('phone'),
                request.form.get('address'),
                int(request.form.get('total_purchase') or 0),
                int(request.form.get('purchase_count') or 0),
                request.form.get('notes'),
                id, current_user.id
            ))
        conn.commit()
        cur.close()
        conn.close()
        flash('顧客情報を更新しました', 'success')
        return redirect(url_for('customers'))
    
    cur.close()
    conn.close()
    return render_template('customer_form.html', customer=dict(customer))

@app.route('/customers/delete/<int:id>')
@login_required
def delete_customer(id):
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("DELETE FROM customers WHERE id = %s AND user_id = %s", (id, current_user.id))
    else:
        cur = conn.cursor()
        cur.execute("DELETE FROM customers WHERE id = ? AND user_id = ?", (id, current_user.id))
    conn.commit()
    conn.close()
    flash('顧客を削除しました', 'info')
    return redirect(url_for('customers'))

# ===================
# 管理者機能
# ===================

@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    """管理者ダッシュボード - 分析ページにリダイレクト"""
    return redirect(url_for('admin_analytics'))

@app.route('/admin/users')
@login_required
@permission_required('users')
def admin_users():
    conn = get_db()
    
    # 検索クエリパラメータを取得
    search_query = request.args.get('search', '').strip()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if search_query:
            # PostgreSQL用の検索クエリ（ユーザー名、表示名、メールで検索）
            cur.execute("""
                SELECT * FROM users 
                WHERE username ILIKE %s 
                   OR display_name ILIKE %s 
                   OR email ILIKE %s
                ORDER BY created_at DESC
            """, (f'%{search_query}%', f'%{search_query}%', f'%{search_query}%'))
        else:
            cur.execute("SELECT * FROM users ORDER BY created_at DESC")
    else:
        cur = conn.cursor()
        if search_query:
            # SQLite用の検索クエリ（ユーザー名、表示名、メールで検索）
            cur.execute("""
                SELECT * FROM users 
                WHERE username LIKE ? 
                   OR display_name LIKE ? 
                   OR email LIKE ?
                ORDER BY created_at DESC
            """, (f'%{search_query}%', f'%{search_query}%', f'%{search_query}%'))
        else:
            cur.execute("SELECT * FROM users ORDER BY created_at DESC")
        
    users = [dict(u) for u in cur.fetchall()]
    
    # 各ユーザーの当月商品数を取得
    for user in users:
        if DATABASE_URL:
            cur.execute("""
                SELECT COUNT(*) as count FROM merchandise 
                WHERE user_id = %s 
                AND DATE_TRUNC('month', COALESCE(purchase_date::date, CURRENT_DATE)) = DATE_TRUNC('month', CURRENT_DATE)
            """, (user['id'],))
            result = cur.fetchone()
            user['monthly_item_count'] = result['count'] if result else 0
        else:
            cur.execute("""
                SELECT COUNT(*) as count FROM merchandise 
                WHERE user_id = ? 
                AND strftime('%Y-%m', COALESCE(purchase_date, date('now'))) = strftime('%Y-%m', 'now')
            """, (user['id'],))
            result = cur.fetchone()
            user['monthly_item_count'] = result[0] if result else 0
        
        # 月額利用料を計算
        count = user['monthly_item_count']
        if count <= 20:
            user['monthly_fee'] = 2500
        elif count <= 50:
            user['monthly_fee'] = 5000
        elif count <= 100:
            user['monthly_fee'] = 10000
        elif count <= 200:
            user['monthly_fee'] = 20000
        elif count <= 300:
            user['monthly_fee'] = 30000
        else:
            user['monthly_fee'] = 30000  # 300以上は30000円
        
        # 開花手数料（sale_typeがnormal以外の手数料合計）
        if DATABASE_URL:
            cur.execute("""
                SELECT COALESCE(SUM(commission), 0) as total_kaika_fee
                FROM merchandise 
                WHERE user_id = %s 
                AND sale_date IS NOT NULL
                AND COALESCE(sale_type, 'normal') != 'normal'
            """, (user['id'],))
            kaika_result = cur.fetchone()
            user['kaika_fee'] = kaika_result['total_kaika_fee'] if kaika_result else 0
        else:
            cur.execute("""
                SELECT COALESCE(SUM(commission), 0) as total_kaika_fee
                FROM merchandise 
                WHERE user_id = ? 
                AND sale_date IS NOT NULL
                AND COALESCE(sale_type, 'normal') != 'normal'
            """, (user['id'],))
            kaika_result = cur.fetchone()
            user['kaika_fee'] = kaika_result[0] if kaika_result else 0
    
    cur.close()
    conn.close()
    
    return render_template('admin/users.html', users=users, search_query=search_query)

@app.route('/admin/users/<int:id>/toggle_admin')
@login_required
@admin_required
def toggle_admin(id):
    """権限を切り替え（後方互換性のため残す）"""
    return redirect(url_for('admin_set_role', id=id, role='toggle'))

@app.route('/admin/users/<int:id>/set_role/<role>')
@login_required
@permission_required('users')
def admin_set_role(id, role):
    """ユーザーの権限を設定"""
    if id == current_user.id:
        flash('自分の権限は変更できません', 'error')
        return redirect(url_for('admin_users'))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT role FROM users WHERE id = %s", (id,))
        user = cur.fetchone()
    else:
        cur = conn.cursor()
        cur.execute("SELECT role FROM users WHERE id = ?", (id,))
        user = cur.fetchone()
    
    if not user:
        flash('ユーザーが見つかりません', 'error')
        cur.close()
        conn.close()
        return redirect(url_for('admin_users'))
    
    current_role = user['role'] if isinstance(user, dict) else user[0]
    
    # toggleの場合は従来の動作
    if role == 'toggle':
        if current_role == 'owner':
            flash('オーナーの権限は変更できません', 'error')
            cur.close()
            conn.close()
            return redirect(url_for('admin_users'))
        new_role = 'user' if current_role == 'admin' else 'admin'
    else:
        # オーナー権限の設定はオーナーのみ可能
        if role == 'owner' and not current_user.is_owner():
            flash('オーナー権限の付与はオーナーのみ可能です', 'error')
            cur.close()
            conn.close()
            return redirect(url_for('admin_users'))
        
        # オーナーの権限変更はオーナーのみ可能
        if current_role == 'owner' and not current_user.is_owner():
            flash('オーナーの権限変更はオーナーのみ可能です', 'error')
            cur.close()
            conn.close()
            return redirect(url_for('admin_users'))
        
        # 有効な権限値かチェック
        if role not in ['owner', 'admin', 'user']:
            flash('無効な権限です', 'error')
            cur.close()
            conn.close()
            return redirect(url_for('admin_users'))
        
        new_role = role
    
    if DATABASE_URL:
        cur.execute("UPDATE users SET role = %s WHERE id = %s", (new_role, id))
    else:
        cur.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    role_names = {'owner': 'オーナー', 'admin': '管理者', 'user': 'ユーザー'}
    flash(f'権限を「{role_names.get(new_role, new_role)}」に変更しました', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:id>/toggle_tuition_exempt')
@login_required
@permission_required('users')
def toggle_tuition_exempt(id):
    """月謝免除を切り替え"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT tuition_exempt FROM users WHERE id = %s", (id,))
        user = cur.fetchone()
        if user:
            current_exempt = user.get('tuition_exempt', False) or False
            new_exempt = not current_exempt
            cur.execute("UPDATE users SET tuition_exempt = %s WHERE id = %s", (new_exempt, id))
    else:
        cur = conn.cursor()
        cur.execute("SELECT tuition_exempt FROM users WHERE id = ?", (id,))
        result = cur.fetchone()
        if result:
            current_exempt = bool(result[0]) if result[0] else False
            new_exempt = not current_exempt
            cur.execute("UPDATE users SET tuition_exempt = ? WHERE id = ?", (1 if new_exempt else 0, id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    if new_exempt:
        flash('月謝免除に設定しました', 'success')
    else:
        flash('月謝免除を解除しました', 'info')
    
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:id>/delete')
@login_required
@permission_required('users')
def delete_user(id):
    if id == current_user.id:
        flash('自分は削除できません', 'error')
        return redirect(url_for('admin_users'))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("DELETE FROM merchandise WHERE user_id = %s", (id,))
        cur.execute("DELETE FROM customers WHERE user_id = %s", (id,))
        cur.execute("DELETE FROM users WHERE id = %s", (id,))
    else:
        cur = conn.cursor()
        cur.execute("DELETE FROM merchandise WHERE user_id = ?", (id,))
        cur.execute("DELETE FROM customers WHERE user_id = ?", (id,))
        cur.execute("DELETE FROM users WHERE id = ?", (id,))
    
    conn.commit()
    conn.close()
    flash('ユーザーを削除しました', 'info')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/add', methods=['GET', 'POST'])
@login_required
@permission_required('users')
def admin_add_user():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        display_name = request.form.get('display_name')
        role = request.form.get('role', 'user')
        
        # 管理者権限の設定を取得
        admin_permissions = request.form.getlist('admin_permissions')
        admin_permissions_json = json.dumps(admin_permissions) if admin_permissions else None
        
        # オーナー権限の付与はオーナーのみ
        if role == 'owner' and not current_user.is_owner():
            role = 'admin'
        
        if not username or not email or not password:
            flash('ユーザー名、メール、パスワードは必須です', 'error')
            return render_template('admin/user_form.html', user=None, permission_options=User.ADMIN_PERMISSION_OPTIONS)
        
        if len(password) < 6:
            flash('パスワードは6文字以上必要です', 'error')
            return render_template('admin/user_form.html', user=None, permission_options=User.ADMIN_PERMISSION_OPTIONS)
        
        conn = get_db()
        try:
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute('''
                    INSERT INTO users (username, email, password_hash, role, display_name, admin_permissions)
                    VALUES (%s, %s, %s, %s, %s, %s)
                ''', (username, email, generate_password_hash(password), role, display_name or username, admin_permissions_json))
            else:
                cur = conn.cursor()
                cur.execute('''
                    INSERT INTO users (username, email, password_hash, role, display_name, admin_permissions)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (username, email, generate_password_hash(password), role, display_name or username, admin_permissions_json))
            conn.commit()
            flash('ユーザーを作成しました', 'success')
            return redirect(url_for('admin_users'))
        except Exception as e:
            flash('ユーザー名またはメールアドレスが既に使用されています', 'error')
        finally:
            conn.close()
    
    return render_template('admin/user_form.html', user=None, permission_options=User.ADMIN_PERMISSION_OPTIONS)

@app.route('/admin/users/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('users')
def admin_edit_user(id):
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE id = %s", (id,))
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = ?", (id,))
    
    user = cur.fetchone()
    if not user:
        flash('ユーザーが見つかりません', 'error')
        return redirect(url_for('admin_users'))
    
    if request.method == 'POST':
        display_name = request.form.get('display_name')
        email = request.form.get('email')
        role = request.form.get('role', 'user')
        new_password = request.form.get('new_password')
        proxy_service_budget_raw = request.form.get('proxy_service_budget', '0')
        tuition_exempt = request.form.get('tuition_exempt') == '1'
        
        # デバッグログ
        print(f"[DEBUG] admin_edit_user: id={id}, new_password入力あり={bool(new_password)}, 長さ={len(new_password) if new_password else 0}")
        print(f"[DEBUG] proxy_service_budget_raw={proxy_service_budget_raw}, type={type(proxy_service_budget_raw)}")
        
        # 金額をint変換
        try:
            proxy_service_budget = int(proxy_service_budget_raw) if proxy_service_budget_raw else 0
        except ValueError:
            proxy_service_budget = 0
        print(f"[DEBUG] proxy_service_budget after conversion={proxy_service_budget}")
        
        # 管理者権限の設定を取得
        admin_permissions = request.form.getlist('admin_permissions')
        admin_permissions_json = json.dumps(admin_permissions) if admin_permissions else None
        
        # 自分の権限は変更不可
        if id == current_user.id:
            role = user['role']
            admin_permissions_json = user.get('admin_permissions')
        
        # オーナー権限の付与はオーナーのみ
        if role == 'owner' and not current_user.is_owner():
            role = user['role']
        
        try:
            print(f"[DEBUG] 更新開始: proxy_service_budget={proxy_service_budget}, tuition_exempt={tuition_exempt}")
            if new_password and len(new_password) >= 6:
                print(f"[DEBUG] パスワード更新実行: user_id={id}")
                hashed_password = generate_password_hash(new_password)
                print(f"[DEBUG] ハッシュ生成完了: {hashed_password[:20]}...")
                if DATABASE_URL:
                    cur.execute('''
                        UPDATE users SET display_name = %s, email = %s, role = %s, password_hash = %s, admin_permissions = %s, proxy_service_budget = %s, tuition_exempt = %s
                        WHERE id = %s
                    ''', (display_name, email, role, hashed_password, admin_permissions_json, proxy_service_budget, tuition_exempt, id))
                else:
                    cur.execute('''
                        UPDATE users SET display_name = ?, email = ?, role = ?, password_hash = ?, admin_permissions = ?, proxy_service_budget = ?, tuition_exempt = ?
                        WHERE id = ?
                    ''', (display_name, email, role, hashed_password, admin_permissions_json, proxy_service_budget, 1 if tuition_exempt else 0, id))
                print(f"[DEBUG] UPDATE実行完了、rowcount={cur.rowcount}")
            else:
                print(f"[DEBUG] パスワード更新なし（空または6文字未満）")
                if DATABASE_URL:
                    cur.execute('''
                        UPDATE users SET display_name = %s, email = %s, role = %s, admin_permissions = %s, proxy_service_budget = %s, tuition_exempt = %s
                        WHERE id = %s
                    ''', (display_name, email, role, admin_permissions_json, proxy_service_budget, tuition_exempt, id))
                else:
                    cur.execute('''
                        UPDATE users SET display_name = ?, email = ?, role = ?, admin_permissions = ?, proxy_service_budget = ?, tuition_exempt = ?
                        WHERE id = ?
                    ''', (display_name, email, role, admin_permissions_json, proxy_service_budget, 1 if tuition_exempt else 0, id))
                print(f"[DEBUG] UPDATE実行完了、rowcount={cur.rowcount}")
            
            conn.commit()
            print(f"[DEBUG] commit完了")
            
            # 更新後の値を確認
            if DATABASE_URL:
                cur.execute("SELECT proxy_service_budget, tuition_exempt FROM users WHERE id = %s", (id,))
            else:
                cur.execute("SELECT proxy_service_budget, tuition_exempt FROM users WHERE id = ?", (id,))
            updated_user = cur.fetchone()
            print(f"[DEBUG] 更新後の値: {updated_user}")
            
            flash('ユーザー情報を更新しました', 'success')
            return redirect(url_for('admin_users'))
        except Exception as e:
            print(f"[ERROR] admin_edit_user例外: {e}")
            import traceback
            traceback.print_exc()
            flash(f'エラーが発生しました: {str(e)}', 'error')
        finally:
            cur.close()
            conn.close()
    else:
        cur.close()
        conn.close()
    
    # admin_permissionsをパースしてテンプレートに渡す
    user_dict = dict(user)
    if user_dict.get('admin_permissions'):
        try:
            user_dict['admin_permissions_list'] = json.loads(user_dict['admin_permissions'])
        except:
            user_dict['admin_permissions_list'] = []
    else:
        user_dict['admin_permissions_list'] = []
    
    return render_template('admin/user_form.html', user=user_dict, permission_options=User.ADMIN_PERMISSION_OPTIONS)

@app.route('/admin/users/<int:id>/items')
@login_required
@permission_required('users')
def admin_user_items(id):
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE id = %s", (id,))
        user = cur.fetchone()
        cur.execute("SELECT * FROM merchandise WHERE user_id = %s ORDER BY id DESC", (id,))
        items = cur.fetchall()
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = ?", (id,))
        user = cur.fetchone()
        cur.execute("SELECT * FROM merchandise WHERE user_id = ? ORDER BY id DESC", (id,))
        items = cur.fetchall()
    
    cur.close()
    conn.close()
    
    if not user:
        flash('ユーザーが見つかりません', 'error')
        return redirect(url_for('admin_users'))
    
    processed_items = []
    for item in items:
        item_dict = dict(item)
        if item_dict.get('photo_path'):
            item_dict['photo_path'] = item_dict['photo_path'].replace('\\', '/')
        if item_dict.get('sale_date'):
            item_dict['profit'] = calculate_profit(
                item_dict.get('sale_price', 0) or 0,
                item_dict.get('purchase_price', 0) or 0,
                item_dict.get('shipping_cost', 0) or 0,
                item_dict.get('commission', 0) or 0
            )
        
        # 削除可能フラグを追加（オーナーは常にTrue、管理者は1日以内のみTrue）
        if current_user.is_owner():
            item_dict['can_delete'] = True
        else:
            created_at = item_dict.get('created_at')
            can_delete = False
            if created_at:
                if isinstance(created_at, str):
                    try:
                        created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                    except:
                        try:
                            created_at = datetime.strptime(created_at[:19], '%Y-%m-%dT%H:%M:%S')
                        except:
                            created_at = None
                if created_at:
                    diff = datetime.now() - created_at
                    can_delete = diff.days < 1
            item_dict['can_delete'] = can_delete
        
        processed_items.append(item_dict)
    
    return render_template('admin/user_items.html', user=dict(user), items=processed_items)

@app.route('/admin/analytics')
@login_required
@permission_required('analytics')
def admin_analytics():
    analytics_data = {}
    widgets = []
    overall_stats = {}
    kaika_fee_data = {}
    
    # 日付フィルター取得
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            # 日付条件を構築
            date_condition = ""
            date_params = []
            if date_from and date_to:
                date_condition = " AND sale_date BETWEEN %s AND %s"
                date_params = [date_from, date_to]
            elif date_from:
                date_condition = " AND sale_date >= %s"
                date_params = [date_from]
            elif date_to:
                date_condition = " AND sale_date <= %s"
                date_params = [date_to]
            
            # 全体統計
            query = """
                SELECT 
                    COUNT(*) as total_items,
                    COALESCE(SUM(purchase_price), 0) as total_purchase,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN sale_price ELSE 0 END), 0) as total_sales,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN 
                        sale_price - purchase_price - shipping_cost - commission ELSE 0 END), 0) as total_profit
                FROM merchandise
                WHERE 1=1 """ + (date_condition.replace("sale_date", "purchase_date") if date_from or date_to else "")
            cur.execute(query, date_params if date_params else None)
            overall_stats = dict(cur.fetchone() or {})
            
            # 開花手数料（sale_typeがnormal以外の手数料合計）
            kaika_query = """
                SELECT 
                    COALESCE(SUM(CASE WHEN sale_type != 'normal' AND sale_type IS NOT NULL THEN commission ELSE 0 END), 0) as total_kaika_fee,
                    COALESCE(SUM(CASE WHEN sale_type = 'photo_packing' THEN commission ELSE 0 END), 0) as photo_packing_fee,
                    COALESCE(SUM(CASE WHEN sale_type = 'wholesale' THEN commission ELSE 0 END), 0) as wholesale_fee,
                    COALESCE(SUM(CASE WHEN sale_type = 'multi_listing' THEN commission ELSE 0 END), 0) as multi_listing_fee,
                    COALESCE(SUM(CASE WHEN sale_type = 'auction' THEN commission ELSE 0 END), 0) as auction_fee,
                    COUNT(CASE WHEN sale_type != 'normal' AND sale_type IS NOT NULL AND sale_date IS NOT NULL THEN 1 END) as kaika_count
                FROM merchandise
                WHERE sale_date IS NOT NULL""" + date_condition
            cur.execute(kaika_query, date_params if date_params else None)
            kaika_fee_data['summary'] = dict(cur.fetchone() or {})
            
            # 開花手数料の月別推移
            kaika_monthly_query = """
                SELECT 
                    TO_CHAR(sale_date, 'YYYY-MM') as month,
                    COALESCE(SUM(CASE WHEN sale_type != 'normal' AND sale_type IS NOT NULL THEN commission ELSE 0 END), 0) as fee,
                    COUNT(CASE WHEN sale_type != 'normal' AND sale_type IS NOT NULL THEN 1 END) as count
                FROM merchandise 
                WHERE sale_date IS NOT NULL""" + date_condition + """
                GROUP BY TO_CHAR(sale_date, 'YYYY-MM')
                ORDER BY month DESC
                LIMIT 12
            """
            cur.execute(kaika_monthly_query, date_params if date_params else None)
            kaika_fee_data['monthly'] = [dict(m) for m in cur.fetchall()]
            
            # ウィジェット設定を取得
            cur.execute("SELECT * FROM widget_settings ORDER BY display_order")
            widgets = cur.fetchall()
            enabled_widgets = {w['widget_key']: w for w in widgets if w['is_enabled']}
            
            # 売上・利益（月別推移）
            if 'sales_profit' in enabled_widgets:
                cur.execute("""
                    SELECT 
                        TO_CHAR(sale_date, 'YYYY-MM') as month,
                        COUNT(*) as count,
                        SUM(sale_price) as sales,
                        SUM(sale_price - purchase_price - shipping_cost - commission) as profit
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                    GROUP BY TO_CHAR(sale_date, 'YYYY-MM')
                    ORDER BY month DESC
                    LIMIT 12
                """)
                analytics_data['monthly_sales'] = [dict(m) for m in cur.fetchall()]
            
            # 売れた商品（Top10）
            if 'top_products' in enabled_widgets:
                cur.execute("""
                    SELECT product_name, brand_name, sale_price, 
                           sale_price - purchase_price - shipping_cost - commission as profit,
                           sale_date
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                    ORDER BY sale_price DESC
                    LIMIT 10
                """)
                analytics_data['top_products'] = [dict(p) for p in cur.fetchall()]
            
            # 売れない商品（在庫滞留）
            if 'slow_products' in enabled_widgets:
                cur.execute("""
                    SELECT product_name, brand_name, purchase_price, purchase_date,
                           CURRENT_DATE - purchase_date::date as days_in_stock
                    FROM merchandise 
                    WHERE sale_date IS NULL AND purchase_date IS NOT NULL
                    ORDER BY days_in_stock DESC
                    LIMIT 10
                """)
                analytics_data['slow_products'] = [dict(p) for p in cur.fetchall()]
            
            # 回転率・在庫日数
            if 'turnover_rate' in enabled_widgets:
                cur.execute("""
                    SELECT 
                        COUNT(*) FILTER (WHERE sale_date IS NOT NULL) as sold_count,
                        COUNT(*) as total_count,
                        AVG(CASE WHEN sale_date IS NOT NULL 
                            THEN sale_date::date - purchase_date::date 
                            ELSE NULL END) as avg_days_to_sell,
                        AVG(CASE WHEN sale_date IS NULL AND purchase_date IS NOT NULL
                            THEN CURRENT_DATE - purchase_date::date 
                            ELSE NULL END) as avg_days_in_stock
                    FROM merchandise 
                    WHERE purchase_date IS NOT NULL
                """)
                analytics_data['turnover_stats'] = dict(cur.fetchone() or {})
            
            # 成約率
            if 'closing_rate' in enabled_widgets:
                cur.execute("""
                    SELECT 
                        COUNT(*) FILTER (WHERE is_listed = TRUE OR is_listed = 1) as listed_count,
                        COUNT(*) FILTER (WHERE sale_date IS NOT NULL) as sold_count,
                        COUNT(*) as total_count
                    FROM merchandise
                """)
                analytics_data['closing_stats'] = dict(cur.fetchone() or {})
            
            # 平均単価
            if 'avg_price' in enabled_widgets:
                cur.execute("""
                    SELECT 
                        AVG(purchase_price) as avg_purchase,
                        AVG(sale_price) FILTER (WHERE sale_date IS NOT NULL) as avg_sale,
                        AVG(sale_price - purchase_price - shipping_cost - commission) FILTER (WHERE sale_date IS NOT NULL) as avg_profit
                    FROM merchandise
                """)
                analytics_data['avg_price_stats'] = dict(cur.fetchone() or {})
            
            # リピート率（顧客別）
            if 'repeat_rate' in enabled_widgets:
                cur.execute("""
                    SELECT 
                        sales_destination,
                        COUNT(*) as purchase_count
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL AND sales_destination IS NOT NULL
                    GROUP BY sales_destination
                    ORDER BY purchase_count DESC
                """)
                analytics_data['repeat_stats'] = [dict(r) for r in cur.fetchall()]
            
            # 時間帯・曜日別売上
            if 'time_sales' in enabled_widgets:
                cur.execute("""
                    SELECT 
                        TO_CHAR(sale_date, 'Day') as day_of_week,
                        EXTRACT(DOW FROM sale_date) as dow_num,
                        COUNT(*) as count,
                        SUM(sale_price) as sales
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                    GROUP BY TO_CHAR(sale_date, 'Day'), EXTRACT(DOW FROM sale_date)
                    ORDER BY dow_num
                """)
                analytics_data['day_sales'] = [dict(d) for d in cur.fetchall()]
            
            # ブランド別統計
            if 'brand_stats' in enabled_widgets:
                cur.execute("""
                    SELECT brand_name, 
                           COUNT(*) as count, 
                           SUM(sale_price) as total_sales,
                           SUM(sale_price - purchase_price - shipping_cost - commission) as total_profit,
                           CASE WHEN SUM(purchase_price) > 0 
                                THEN ROUND(SUM(sale_price - purchase_price - shipping_cost - commission)::numeric / SUM(purchase_price) * 100, 1)
                                ELSE 0 END as profit_rate
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL AND brand_name IS NOT NULL AND brand_name != ''
                    GROUP BY brand_name
                    ORDER BY profit_rate DESC
                    LIMIT 10
                """)
                analytics_data['brand_stats'] = [dict(b) for b in cur.fetchall()]
            
            # 販売先別統計
            if 'destination_stats' in enabled_widgets:
                cur.execute("""
                    SELECT sales_destination, COUNT(*) as count, SUM(sale_price) as total_sales
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL AND sales_destination IS NOT NULL
                    GROUP BY sales_destination
                    ORDER BY total_sales DESC
                """)
                analytics_data['destination_stats'] = [dict(d) for d in cur.fetchall()]
        
        else:
            cur = conn.cursor()
            cur.row_factory = sqlite3.Row
            
            # 日付条件を構築（SQLite）
            date_condition_sqlite = ""
            date_params_sqlite = []
            if date_from and date_to:
                date_condition_sqlite = " AND sale_date BETWEEN ? AND ?"
                date_params_sqlite = [date_from, date_to]
            elif date_from:
                date_condition_sqlite = " AND sale_date >= ?"
                date_params_sqlite = [date_from]
            elif date_to:
                date_condition_sqlite = " AND sale_date <= ?"
                date_params_sqlite = [date_to]
            
            # 全体統計
            cur.execute("""
                SELECT 
                    COUNT(*) as total_items,
                    COALESCE(SUM(purchase_price), 0) as total_purchase,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN sale_price ELSE 0 END), 0) as total_sales,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN 
                        sale_price - purchase_price - shipping_cost - commission ELSE 0 END), 0) as total_profit
                FROM merchandise
            """)
            overall_stats = dict(cur.fetchone() or {})
            
            # 開花手数料（sale_typeがnormal以外の手数料合計）
            cur.execute("""
                SELECT 
                    COALESCE(SUM(CASE WHEN sale_type != 'normal' AND sale_type IS NOT NULL THEN commission ELSE 0 END), 0) as total_kaika_fee,
                    COALESCE(SUM(CASE WHEN sale_type = 'photo_packing' THEN commission ELSE 0 END), 0) as photo_packing_fee,
                    COALESCE(SUM(CASE WHEN sale_type = 'wholesale' THEN commission ELSE 0 END), 0) as wholesale_fee,
                    COALESCE(SUM(CASE WHEN sale_type = 'multi_listing' THEN commission ELSE 0 END), 0) as multi_listing_fee,
                    COALESCE(SUM(CASE WHEN sale_type = 'auction' THEN commission ELSE 0 END), 0) as auction_fee,
                    SUM(CASE WHEN sale_type != 'normal' AND sale_type IS NOT NULL AND sale_date IS NOT NULL THEN 1 ELSE 0 END) as kaika_count
                FROM merchandise
                WHERE sale_date IS NOT NULL""" + date_condition_sqlite, date_params_sqlite if date_params_sqlite else [])
            kaika_fee_data['summary'] = dict(cur.fetchone() or {})
            
            # 開花手数料の月別推移
            cur.execute("""
                SELECT 
                    strftime('%Y-%m', sale_date) as month,
                    COALESCE(SUM(CASE WHEN sale_type != 'normal' AND sale_type IS NOT NULL THEN commission ELSE 0 END), 0) as fee,
                    SUM(CASE WHEN sale_type != 'normal' AND sale_type IS NOT NULL THEN 1 ELSE 0 END) as count
                FROM merchandise 
                WHERE sale_date IS NOT NULL""" + date_condition_sqlite + """
                GROUP BY strftime('%Y-%m', sale_date)
                ORDER BY month DESC
                LIMIT 12
            """, date_params_sqlite if date_params_sqlite else [])
            kaika_fee_data['monthly'] = [dict(m) for m in cur.fetchall()]
            
            # ウィジェット設定を取得
            cur.execute("SELECT * FROM widget_settings ORDER BY display_order")
            widgets = [dict(w) for w in cur.fetchall()]
            enabled_widgets = {w['widget_key']: w for w in widgets if w['is_enabled']}
            
            # 売上・利益（月別推移）
            if 'sales_profit' in enabled_widgets:
                cur.execute("""
                    SELECT 
                        strftime('%Y-%m', sale_date) as month,
                        COUNT(*) as count,
                        SUM(sale_price) as sales,
                        SUM(sale_price - purchase_price - shipping_cost - commission) as profit
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                    GROUP BY strftime('%Y-%m', sale_date)
                    ORDER BY month DESC
                    LIMIT 12
                """)
                analytics_data['monthly_sales'] = [dict(m) for m in cur.fetchall()]
            
            # 売れた商品（Top10）
            if 'top_products' in enabled_widgets:
                cur.execute("""
                    SELECT product_name, brand_name, sale_price, 
                           sale_price - purchase_price - shipping_cost - commission as profit,
                           sale_date
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                    ORDER BY sale_price DESC
                    LIMIT 10
                """)
                analytics_data['top_products'] = [dict(p) for p in cur.fetchall()]
            
            # 売れない商品（在庫滞留）
            if 'slow_products' in enabled_widgets:
                cur.execute("""
                    SELECT product_name, brand_name, purchase_price, purchase_date,
                           julianday('now') - julianday(purchase_date) as days_in_stock
                    FROM merchandise 
                    WHERE sale_date IS NULL AND purchase_date IS NOT NULL
                    ORDER BY days_in_stock DESC
                    LIMIT 10
                """)
                analytics_data['slow_products'] = [dict(p) for p in cur.fetchall()]
            
            # 回転率・在庫日数
            if 'turnover_rate' in enabled_widgets:
                cur.execute("""
                    SELECT 
                        SUM(CASE WHEN sale_date IS NOT NULL THEN 1 ELSE 0 END) as sold_count,
                        COUNT(*) as total_count,
                        AVG(CASE WHEN sale_date IS NOT NULL 
                            THEN julianday(sale_date) - julianday(purchase_date) 
                            ELSE NULL END) as avg_days_to_sell,
                        AVG(CASE WHEN sale_date IS NULL AND purchase_date IS NOT NULL
                            THEN julianday('now') - julianday(purchase_date) 
                            ELSE NULL END) as avg_days_in_stock
                    FROM merchandise 
                    WHERE purchase_date IS NOT NULL
                """)
                analytics_data['turnover_stats'] = dict(cur.fetchone() or {})
            
            # 成約率
            if 'closing_rate' in enabled_widgets:
                cur.execute("""
                    SELECT 
                        SUM(CASE WHEN is_listed = 1 THEN 1 ELSE 0 END) as listed_count,
                        SUM(CASE WHEN sale_date IS NOT NULL THEN 1 ELSE 0 END) as sold_count,
                        COUNT(*) as total_count
                    FROM merchandise
                """)
                analytics_data['closing_stats'] = dict(cur.fetchone() or {})
            
            # 平均単価
            if 'avg_price' in enabled_widgets:
                cur.execute("""
                    SELECT 
                        AVG(purchase_price) as avg_purchase,
                        AVG(CASE WHEN sale_date IS NOT NULL THEN sale_price ELSE NULL END) as avg_sale,
                        AVG(CASE WHEN sale_date IS NOT NULL THEN sale_price - purchase_price - shipping_cost - commission ELSE NULL END) as avg_profit
                    FROM merchandise
                """)
                analytics_data['avg_price_stats'] = dict(cur.fetchone() or {})
            
            # リピート率
            if 'repeat_rate' in enabled_widgets:
                cur.execute("""
                    SELECT 
                        sales_destination,
                        COUNT(*) as purchase_count
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL AND sales_destination IS NOT NULL
                    GROUP BY sales_destination
                    ORDER BY purchase_count DESC
                """)
                analytics_data['repeat_stats'] = [dict(r) for r in cur.fetchall()]
            
            # 時間帯・曜日別売上
            if 'time_sales' in enabled_widgets:
                cur.execute("""
                    SELECT 
                        CASE strftime('%w', sale_date)
                            WHEN '0' THEN '日曜日'
                            WHEN '1' THEN '月曜日'
                            WHEN '2' THEN '火曜日'
                            WHEN '3' THEN '水曜日'
                            WHEN '4' THEN '木曜日'
                            WHEN '5' THEN '金曜日'
                            WHEN '6' THEN '土曜日'
                        END as day_of_week,
                        strftime('%w', sale_date) as dow_num,
                        COUNT(*) as count,
                        SUM(sale_price) as sales
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL
                    GROUP BY strftime('%w', sale_date)
                    ORDER BY dow_num
                """)
                analytics_data['day_sales'] = [dict(d) for d in cur.fetchall()]
            
            # ブランド別統計
            if 'brand_stats' in enabled_widgets:
                cur.execute("""
                    SELECT brand_name, 
                           COUNT(*) as count, 
                           SUM(sale_price) as total_sales,
                           SUM(sale_price - purchase_price - shipping_cost - commission) as total_profit,
                           CASE WHEN SUM(purchase_price) > 0 
                                THEN ROUND(CAST(SUM(sale_price - purchase_price - shipping_cost - commission) AS REAL) / SUM(purchase_price) * 100, 1)
                                ELSE 0 END as profit_rate
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL AND brand_name IS NOT NULL AND brand_name != ''
                    GROUP BY brand_name
                    ORDER BY profit_rate DESC
                    LIMIT 10
                """)
                analytics_data['brand_stats'] = [dict(b) for b in cur.fetchall()]
            
            # 販売先別統計
            if 'destination_stats' in enabled_widgets:
                cur.execute("""
                    SELECT sales_destination, COUNT(*) as count, SUM(sale_price) as total_sales
                    FROM merchandise 
                    WHERE sale_date IS NOT NULL AND sales_destination IS NOT NULL
                    GROUP BY sales_destination
                    ORDER BY total_sales DESC
                """)
                analytics_data['destination_stats'] = [dict(d) for d in cur.fetchall()]
        
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Analytics error: {e}")
        import traceback
        traceback.print_exc()
    
    return render_template('admin/analytics.html',
                         widgets=[dict(w) for w in widgets] if widgets else [],
                         overall_stats=overall_stats,
                         kaika_fee=kaika_fee_data,
                         date_from=date_from,
                         date_to=date_to,
                         **analytics_data)

@app.route('/admin/analytics/kaika')
@login_required
@admin_required
def admin_analytics_kaika():
    """開花（管理者）商品の分析ページ（管理者/オーナーの商品）"""
    analytics_data = {}
    overall_stats = {}
    
    # 日付フィルター取得
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            # 管理者/オーナーのユーザーIDを取得
            cur.execute("SELECT id FROM users WHERE role IN ('admin', 'owner')")
            admin_ids = [row['id'] for row in cur.fetchall()]
            
            # 日付条件を構築
            date_condition = ""
            date_params = []
            if date_from and date_to:
                date_condition = " AND sale_date BETWEEN %s AND %s"
                date_params = [date_from, date_to]
            elif date_from:
                date_condition = " AND sale_date >= %s"
                date_params = [date_from]
            elif date_to:
                date_condition = " AND sale_date <= %s"
                date_params = [date_to]
            
            # 管理者商品の条件（user_id IS NULL または 管理者/オーナーのID）
            if admin_ids:
                admin_placeholders = ','.join(['%s'] * len(admin_ids))
                user_condition = f"(user_id IS NULL OR user_id IN ({admin_placeholders}))"
                base_params = admin_ids
            else:
                user_condition = "user_id IS NULL"
                base_params = []
            
            # 全体統計
            query = f"""
                SELECT 
                    COUNT(*) as total_items,
                    COALESCE(SUM(purchase_price), 0) as total_purchase,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN sale_price ELSE 0 END), 0) as total_sales,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN 
                        sale_price - purchase_price - shipping_cost - commission ELSE 0 END), 0) as total_profit
                FROM merchandise
                WHERE {user_condition} """ + (date_condition.replace("sale_date", "purchase_date") if date_from or date_to else "")
            cur.execute(query, base_params + date_params if (base_params or date_params) else None)
            overall_stats = dict(cur.fetchone() or {})
            
            # 月別売上・利益推移
            cur.execute(f"""
                SELECT 
                    TO_CHAR(sale_date, 'YYYY-MM') as month,
                    COUNT(*) as count,
                    COALESCE(SUM(sale_price), 0) as sales,
                    COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as profit
                FROM merchandise 
                WHERE {user_condition} AND sale_date IS NOT NULL {date_condition}
                GROUP BY TO_CHAR(sale_date, 'YYYY-MM')
                ORDER BY month DESC
                LIMIT 12
            """, base_params + date_params if (base_params or date_params) else None)
            analytics_data['monthly_sales'] = [dict(m) for m in cur.fetchall()]
            
            # ブランド別統計
            cur.execute(f"""
                SELECT 
                    COALESCE(brand_name, '(ブランド名なし)') as brand_name,
                    COUNT(*) as count,
                    COALESCE(SUM(sale_price), 0) as total_sales,
                    COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as total_profit,
                    CASE WHEN SUM(sale_price) > 0 
                        THEN ROUND(SUM(sale_price - purchase_price - shipping_cost - commission) * 100.0 / SUM(sale_price), 1)
                        ELSE 0 END as profit_rate
                FROM merchandise 
                WHERE {user_condition} AND sale_date IS NOT NULL {date_condition}
                GROUP BY brand_name
                ORDER BY total_profit DESC
                LIMIT 10
            """, base_params + date_params if (base_params or date_params) else None)
            analytics_data['brand_stats'] = [dict(b) for b in cur.fetchall()]
            
            # 販売タイプ別統計
            cur.execute(f"""
                SELECT 
                    COALESCE(sale_type, 'normal') as sale_type,
                    COUNT(*) as count,
                    COALESCE(SUM(sale_price), 0) as total_sales,
                    COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as total_profit
                FROM merchandise 
                WHERE {user_condition} AND sale_date IS NOT NULL {date_condition}
                GROUP BY sale_type
                ORDER BY count DESC
            """, base_params + date_params if (base_params or date_params) else None)
            analytics_data['sale_type_stats'] = [dict(s) for s in cur.fetchall()]
            
            # 在庫統計
            cur.execute(f"""
                SELECT 
                    COUNT(*) as unsold_count,
                    COALESCE(SUM(purchase_price), 0) as inventory_value
                FROM merchandise 
                WHERE {user_condition} AND sale_date IS NULL
            """, base_params if base_params else None)
            analytics_data['inventory'] = dict(cur.fetchone() or {})
            
        else:
            import sqlite3
            cur = conn.cursor()
            conn.row_factory = sqlite3.Row
            
            # 管理者/オーナーのユーザーIDを取得
            cur.execute("SELECT id FROM users WHERE role IN ('admin', 'owner')")
            admin_ids = [row[0] for row in cur.fetchall()]
            
            # 日付条件を構築（SQLite用）
            date_condition = ""
            date_params = []
            if date_from and date_to:
                date_condition = " AND sale_date BETWEEN ? AND ?"
                date_params = [date_from, date_to]
            elif date_from:
                date_condition = " AND sale_date >= ?"
                date_params = [date_from]
            elif date_to:
                date_condition = " AND sale_date <= ?"
                date_params = [date_to]
            
            # 管理者商品の条件（user_id IS NULL または 管理者/オーナーのID）
            if admin_ids:
                admin_placeholders = ','.join(['?'] * len(admin_ids))
                user_condition = f"(user_id IS NULL OR user_id IN ({admin_placeholders}))"
                base_params = admin_ids
            else:
                user_condition = "user_id IS NULL"
                base_params = []
            
            # 全体統計
            query = f"""
                SELECT 
                    COUNT(*) as total_items,
                    COALESCE(SUM(purchase_price), 0) as total_purchase,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN sale_price ELSE 0 END), 0) as total_sales,
                    COALESCE(SUM(CASE WHEN sale_date IS NOT NULL THEN 
                        sale_price - purchase_price - shipping_cost - commission ELSE 0 END), 0) as total_profit
                FROM merchandise
                WHERE {user_condition} """ + (date_condition.replace("sale_date", "purchase_date") if date_from or date_to else "")
            cur.execute(query, base_params + date_params if (base_params or date_params) else ())
            overall_stats = dict(cur.fetchone() or {})
            
            # 月別売上・利益推移
            cur.execute(f"""
                SELECT 
                    strftime('%Y-%m', sale_date) as month,
                    COUNT(*) as count,
                    COALESCE(SUM(sale_price), 0) as sales,
                    COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as profit
                FROM merchandise 
                WHERE {user_condition} AND sale_date IS NOT NULL {date_condition}
                GROUP BY strftime('%Y-%m', sale_date)
                ORDER BY month DESC
                LIMIT 12
            """, base_params + date_params if (base_params or date_params) else ())
            analytics_data['monthly_sales'] = [dict(m) for m in cur.fetchall()]
            
            # ブランド別統計
            cur.execute(f"""
                SELECT 
                    COALESCE(brand_name, '(ブランド名なし)') as brand_name,
                    COUNT(*) as count,
                    COALESCE(SUM(sale_price), 0) as total_sales,
                    COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as total_profit,
                    CASE WHEN SUM(sale_price) > 0 
                        THEN ROUND(SUM(sale_price - purchase_price - shipping_cost - commission) * 100.0 / SUM(sale_price), 1)
                        ELSE 0 END as profit_rate
                FROM merchandise 
                WHERE {user_condition} AND sale_date IS NOT NULL {date_condition}
                GROUP BY brand_name
                ORDER BY total_profit DESC
                LIMIT 10
            """, base_params + date_params if (base_params or date_params) else ())
            analytics_data['brand_stats'] = [dict(b) for b in cur.fetchall()]
            
            # 販売タイプ別統計
            cur.execute(f"""
                SELECT 
                    COALESCE(sale_type, 'normal') as sale_type,
                    COUNT(*) as count,
                    COALESCE(SUM(sale_price), 0) as total_sales,
                    COALESCE(SUM(sale_price - purchase_price - shipping_cost - commission), 0) as total_profit
                FROM merchandise 
                WHERE {user_condition} AND sale_date IS NOT NULL {date_condition}
                GROUP BY sale_type
                ORDER BY count DESC
            """, base_params + date_params if (base_params or date_params) else ())
            analytics_data['sale_type_stats'] = [dict(s) for s in cur.fetchall()]
            
            # 在庫統計
            cur.execute(f"""
                SELECT 
                    COUNT(*) as unsold_count,
                    COALESCE(SUM(purchase_price), 0) as inventory_value
                FROM merchandise 
                WHERE {user_condition} AND sale_date IS NULL
            """, base_params if base_params else ())
            analytics_data['inventory'] = dict(cur.fetchone() or {})
        
        cur.close()
        conn.close()
        
    except Exception as e:
        print(f"Admin analytics kaika error: {e}")
        import traceback
        traceback.print_exc()
    
    # デフォルト値を設定（エラー時も表示されるように）
    if not overall_stats:
        overall_stats = {'total_items': 0, 'total_purchase': 0, 'total_sales': 0, 'total_profit': 0}
    
    return render_template('admin/analytics_kaika.html',
                         overall_stats=overall_stats,
                         date_from=date_from,
                         date_to=date_to,
                         **analytics_data)

@app.route('/admin/analytics/settings', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_analytics_settings():
    conn = get_db()
    
    if request.method == 'POST':
        if DATABASE_URL:
            cur = conn.cursor()
            # すべてのウィジェットを無効化
            cur.execute("UPDATE widget_settings SET is_enabled = FALSE")
            # 選択されたウィジェットを有効化
            enabled = request.form.getlist('enabled_widgets')
            for widget_key in enabled:
                cur.execute("UPDATE widget_settings SET is_enabled = TRUE WHERE widget_key = %s", (widget_key,))
            
            # 表示順を更新
            order_data = request.form.get('widget_order', '')
            if order_data:
                for i, widget_key in enumerate(order_data.split(',')):
                    cur.execute("UPDATE widget_settings SET display_order = %s WHERE widget_key = %s", (i + 1, widget_key.strip()))
        else:
            cur = conn.cursor()
            cur.execute("UPDATE widget_settings SET is_enabled = 0")
            enabled = request.form.getlist('enabled_widgets')
            for widget_key in enabled:
                cur.execute("UPDATE widget_settings SET is_enabled = 1 WHERE widget_key = ?", (widget_key,))
            
            order_data = request.form.get('widget_order', '')
            if order_data:
                for i, widget_key in enumerate(order_data.split(',')):
                    cur.execute("UPDATE widget_settings SET display_order = ? WHERE widget_key = ?", (i + 1, widget_key.strip()))
        
        conn.commit()
        cur.close()
        conn.close()
        flash('ウィジェット設定を保存しました', 'success')
        return redirect(url_for('admin_analytics'))
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
    
    cur.execute("SELECT * FROM widget_settings ORDER BY display_order")
    widgets = cur.fetchall()
    cur.close()
    conn.close()
    
    return render_template('admin/analytics_settings.html', widgets=[dict(w) for w in widgets])

# ===================
# 代行仕入れサービス管理（オーナー専用）
# ===================

@app.route('/admin/proxy-service')
@login_required
def admin_proxy_service():
    """代行仕入れサービス管理画面 - オークション一覧（オーナー・管理者）"""
    if not (current_user.is_owner() or current_user.is_admin()):
        flash('この機能はオーナーまたは管理者のみ利用可能です', 'error')
        return redirect(url_for('index'))
    
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # 全オークションを取得（最大5個まで）
            cur.execute("""
                SELECT ps.*, 
                       (SELECT COUNT(*) FROM merchandise m WHERE m.auction_id = ps.id AND m.sale_date IS NULL) as item_count,
                       (SELECT COUNT(*) FROM proxy_service_bids b 
                        JOIN merchandise m ON b.merchandise_id = m.id 
                        WHERE m.auction_id = ps.id) as bid_count
                FROM proxy_service_settings ps
                ORDER BY ps.id DESC
            """)
            auctions = [dict(a) for a in cur.fetchall()]
            
            # 全ユーザーを取得
            cur.execute("""
                SELECT u.id, u.username, u.display_name, u.role,
                       CASE WHEN psu.user_id IS NOT NULL THEN TRUE ELSE FALSE END as is_selected
                FROM users u
                LEFT JOIN proxy_service_users psu ON u.id = psu.user_id
                ORDER BY 
                    CASE u.role WHEN 'owner' THEN 1 WHEN 'admin' THEN 2 ELSE 3 END,
                    u.id
            """)
            users = cur.fetchall()
        else:
            import sqlite3
            cur = conn.cursor()
            conn.row_factory = sqlite3.Row
            # 全オークションを取得
            cur.execute("""
                SELECT ps.*, 
                       (SELECT COUNT(*) FROM merchandise m WHERE m.auction_id = ps.id AND m.sale_date IS NULL) as item_count,
                       (SELECT COUNT(*) FROM proxy_service_bids b 
                        JOIN merchandise m ON b.merchandise_id = m.id 
                        WHERE m.auction_id = ps.id) as bid_count
                FROM proxy_service_settings ps
                ORDER BY ps.id DESC
            """)
            auctions = [dict(a) for a in cur.fetchall()]
            
            # 全ユーザーを取得
            cur.execute("""
                SELECT u.id, u.username, u.display_name, u.role,
                       CASE WHEN psu.user_id IS NOT NULL THEN 1 ELSE 0 END as is_selected
                FROM users u
                LEFT JOIN proxy_service_users psu ON u.id = psu.user_id
                ORDER BY 
                    CASE u.role WHEN 'owner' THEN 1 WHEN 'admin' THEN 2 ELSE 3 END,
                    u.id
            """)
            users = cur.fetchall()
        
        cur.close()
        conn.close()
        
        # 各オークションの状態を判定（終了/開始前/公開中）
        now = datetime.now()
        for auction in auctions:
            end_dt = auction.get('end_datetime')
            start_dt = auction.get('start_datetime')
            
            # 終了判定
            if end_dt:
                if isinstance(end_dt, str):
                    try:
                        end_dt = datetime.fromisoformat(end_dt.replace('T', ' ').split('.')[0])
                    except:
                        end_dt = None
                if end_dt and now > end_dt:
                    auction['is_ended'] = True
                else:
                    auction['is_ended'] = False
            else:
                auction['is_ended'] = False
            
            # 開始前判定
            if start_dt:
                if isinstance(start_dt, str):
                    try:
                        start_dt = datetime.fromisoformat(start_dt.replace('T', ' ').split('.')[0])
                    except:
                        start_dt = None
                if start_dt and now < start_dt:
                    auction['is_not_started'] = True
                else:
                    auction['is_not_started'] = False
            else:
                auction['is_not_started'] = False
        
        return render_template('admin/proxy_service_list.html',
                             auctions=auctions,
                             users=[dict(u) for u in users],
                             max_auctions=5)
    except Exception as e:
        import traceback
        print(f"Proxy service admin error: {e}")
        traceback.print_exc()
        flash(f'エラーが発生しました: {str(e)}', 'error')
        return redirect(url_for('index'))

@app.route('/admin/proxy-service/create', methods=['GET', 'POST'])
@login_required
def admin_proxy_service_create():
    """新規オークション作成"""
    if not (current_user.is_owner() or current_user.is_admin()):
        flash('この機能はオーナーまたは管理者のみ利用可能です', 'error')
        return redirect(url_for('index'))
    
    conn = get_db()
    
    # オークション数チェック（最大5個まで）
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM proxy_service_settings")
        count = cur.fetchone()[0]
        if count >= 5:
            flash('オークションは最大5個までです', 'error')
            return redirect(url_for('admin_proxy_service'))
        cur.close()
    else:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM proxy_service_settings")
        count = cur.fetchone()[0]
        if count >= 5:
            flash('オークションは最大5個までです', 'error')
            return redirect(url_for('admin_proxy_service'))
        cur.close()
    
    if request.method == 'POST':
        auction_name = request.form.get('auction_name', 'オークション')
        page_title = request.form.get('page_title', '代行仕入れサービス')
        page_description = request.form.get('page_description', '')
        start_datetime = request.form.get('start_datetime', '') or None
        end_datetime = request.form.get('end_datetime', '') or None
        sale_mode = request.form.get('sale_mode', 'auction')
        is_public = request.form.get('is_public') == 'on'
        
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO proxy_service_settings 
                (auction_name, page_title, page_description, start_datetime, end_datetime, sale_mode, is_public, updated_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (auction_name, page_title, page_description, start_datetime, end_datetime, sale_mode, is_public, current_user.id))
            new_id = cur.fetchone()[0]
            conn.commit()
        else:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO proxy_service_settings 
                (auction_name, page_title, page_description, start_datetime, end_datetime, sale_mode, is_public, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (auction_name, page_title, page_description, start_datetime, end_datetime, sale_mode, 1 if is_public else 0, current_user.id))
            new_id = cur.lastrowid
            conn.commit()
        
        cur.close()
        conn.close()
        
        flash(f'オークション「{auction_name}」を作成しました', 'success')
        return redirect(url_for('admin_proxy_service_detail', auction_id=new_id))
    
    conn.close()
    return render_template('admin/proxy_service_create.html')

@app.route('/admin/proxy-service/<int:auction_id>')
@login_required
def admin_proxy_service_detail(auction_id):
    """個別オークション詳細・編集画面"""
    if not (current_user.is_owner() or current_user.is_admin()):
        flash('この機能はオーナーまたは管理者のみ利用可能です', 'error')
        return redirect(url_for('index'))
    
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # オークション設定取得
            cur.execute("SELECT * FROM proxy_service_settings WHERE id = %s", (auction_id,))
            settings = cur.fetchone()
            if not settings:
                flash('オークションが見つかりません', 'error')
                return redirect(url_for('admin_proxy_service'))
            
            # 全ユーザーを取得
            cur.execute("""
                SELECT u.id, u.username, u.display_name, u.role,
                       CASE WHEN psu.user_id IS NOT NULL THEN TRUE ELSE FALSE END as is_selected
                FROM users u
                LEFT JOIN proxy_service_users psu ON u.id = psu.user_id
                ORDER BY 
                    CASE u.role WHEN 'owner' THEN 1 WHEN 'admin' THEN 2 ELSE 3 END,
                    u.id
            """)
            users = cur.fetchall()
            
            # このオークションに掲載中の商品を取得
            cur.execute("""
                SELECT m.*, COALESCE(u.display_name, '不明') as owner_name,
                       (SELECT bid_amount FROM proxy_service_bids WHERE merchandise_id = m.id ORDER BY bid_amount DESC LIMIT 1) as highest_bid,
                       (SELECT bidder_name FROM proxy_service_bids WHERE merchandise_id = m.id ORDER BY bid_amount DESC LIMIT 1) as highest_bidder,
                       (SELECT COUNT(*) FROM proxy_service_bids WHERE merchandise_id = m.id) as bid_count
                FROM merchandise m
                LEFT JOIN users u ON m.user_id = u.id
                WHERE m.auction_id = %s AND m.sale_date IS NULL
                ORDER BY m.id DESC
            """, (auction_id,))
            items = cur.fetchall()
            
            # 入札履歴を取得
            cur.execute("""
                SELECT b.*, m.product_name
                FROM proxy_service_bids b
                JOIN merchandise m ON b.merchandise_id = m.id
                WHERE m.auction_id = %s
                ORDER BY b.created_at DESC
                LIMIT 50
            """, (auction_id,))
            bids = cur.fetchall()
            
            # 選択可能な商品（未販売商品で、他のオークションに属していないもの）
            cur.execute("""
                SELECT m.id, m.product_name, m.brand_name, m.listing_price, m.photo_path, 
                       COALESCE(u.display_name, '不明') as owner_name,
                       m.auction_id
                FROM merchandise m
                LEFT JOIN users u ON m.user_id = u.id
                WHERE m.sale_date IS NULL
                  AND (m.auction_id IS NULL OR m.auction_id = %s)
                ORDER BY m.id DESC
                LIMIT 100
            """, (auction_id,))
            available_items = cur.fetchall()
        else:
            import sqlite3
            cur = conn.cursor()
            conn.row_factory = sqlite3.Row
            # オークション設定取得
            cur.execute("SELECT * FROM proxy_service_settings WHERE id = ?", (auction_id,))
            settings = cur.fetchone()
            if not settings:
                flash('オークションが見つかりません', 'error')
                return redirect(url_for('admin_proxy_service'))
            
            # 全ユーザーを取得
            cur.execute("""
                SELECT u.id, u.username, u.display_name, u.role,
                       CASE WHEN psu.user_id IS NOT NULL THEN 1 ELSE 0 END as is_selected
                FROM users u
                LEFT JOIN proxy_service_users psu ON u.id = psu.user_id
                ORDER BY 
                    CASE u.role WHEN 'owner' THEN 1 WHEN 'admin' THEN 2 ELSE 3 END,
                    u.id
            """)
            users = cur.fetchall()
            
            # このオークションに掲載中の商品を取得
            cur.execute("""
                SELECT m.*, COALESCE(u.display_name, '不明') as owner_name,
                       (SELECT bid_amount FROM proxy_service_bids WHERE merchandise_id = m.id ORDER BY bid_amount DESC LIMIT 1) as highest_bid,
                       (SELECT bidder_name FROM proxy_service_bids WHERE merchandise_id = m.id ORDER BY bid_amount DESC LIMIT 1) as highest_bidder,
                       (SELECT COUNT(*) FROM proxy_service_bids WHERE merchandise_id = m.id) as bid_count
                FROM merchandise m
                LEFT JOIN users u ON m.user_id = u.id
                WHERE m.auction_id = ? AND m.sale_date IS NULL
                ORDER BY m.id DESC
            """, (auction_id,))
            items = cur.fetchall()
            
            # 入札履歴を取得
            cur.execute("""
                SELECT b.*, m.product_name
                FROM proxy_service_bids b
                JOIN merchandise m ON b.merchandise_id = m.id
                WHERE m.auction_id = ?
                ORDER BY b.created_at DESC
                LIMIT 50
            """, (auction_id,))
            bids = cur.fetchall()
            
            # 選択可能な商品（未販売商品で、他のオークションに属していないもの）
            cur.execute("""
                SELECT m.id, m.product_name, m.brand_name, m.listing_price, m.photo_path, 
                       COALESCE(u.display_name, '不明') as owner_name,
                       m.auction_id
                FROM merchandise m
                LEFT JOIN users u ON m.user_id = u.id
                WHERE m.sale_date IS NULL
                  AND (m.auction_id IS NULL OR m.auction_id = ?)
                ORDER BY m.id DESC
                LIMIT 100
            """, (auction_id,))
            available_items = cur.fetchall()
        
        cur.close()
        conn.close()
        
        return render_template('admin/proxy_service.html',
                             settings=dict(settings) if settings else {},
                             users=[dict(u) for u in users],
                             items=[dict(i) for i in items],
                             bids=[dict(b) for b in bids],
                             available_items=[dict(i) for i in available_items],
                             auction_id=auction_id)
    except Exception as e:
        import traceback
        print(f"Proxy service detail error: {e}")
        traceback.print_exc()
        flash(f'エラーが発生しました: {str(e)}', 'error')
        return redirect(url_for('admin_proxy_service'))

@app.route('/admin/proxy-service/<int:auction_id>/delete', methods=['POST'])
@login_required
def admin_proxy_service_delete(auction_id):
    """オークション削除"""
    if not (current_user.is_owner() or current_user.is_admin()):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        # 関連する商品のauction_idをNULLに
        cur.execute("UPDATE merchandise SET auction_id = NULL, show_in_proxy_service = FALSE WHERE auction_id = %s", (auction_id,))
        # オークション削除
        cur.execute("DELETE FROM proxy_service_settings WHERE id = %s", (auction_id,))
        conn.commit()
    else:
        cur = conn.cursor()
        cur.execute("UPDATE merchandise SET auction_id = NULL, show_in_proxy_service = 0 WHERE auction_id = ?", (auction_id,))
        cur.execute("DELETE FROM proxy_service_settings WHERE id = ?", (auction_id,))
        conn.commit()
    
    cur.close()
    conn.close()
    
    flash('オークションを削除しました', 'success')
    return redirect(url_for('admin_proxy_service'))

@app.route('/admin/proxy-service/<int:auction_id>/settings', methods=['POST'])
@login_required
def admin_proxy_service_settings(auction_id):
    """代行サービス設定を更新"""
    if not (current_user.is_owner() or current_user.is_admin()):
        flash('この機能はオーナーまたは管理者のみ利用可能です', 'error')
        return redirect(url_for('index'))
    
    is_public = request.form.get('is_public') == 'on'
    auction_name = request.form.get('auction_name', 'オークション')
    page_title = request.form.get('page_title', '代行仕入れサービス')
    page_description = request.form.get('page_description', '')
    start_datetime = request.form.get('start_datetime', '') or None
    end_datetime = request.form.get('end_datetime', '') or None
    sale_mode = request.form.get('sale_mode', 'auction')  # auction or fixed
    selected_users = request.form.getlist('selected_users')
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        # 設定更新
        cur.execute("""
            UPDATE proxy_service_settings 
            SET is_public = %s, auction_name = %s, page_title = %s, page_description = %s,
                start_datetime = %s, end_datetime = %s, sale_mode = %s,
                updated_by = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (is_public, auction_name, page_title, page_description, start_datetime, end_datetime, sale_mode, current_user.id, auction_id))
        
        # ユーザー選択を更新（共通）
        cur.execute("DELETE FROM proxy_service_users")
        for user_id in selected_users:
            cur.execute("INSERT INTO proxy_service_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (int(user_id),))
    else:
        cur = conn.cursor()
        cur.execute("""
            UPDATE proxy_service_settings 
            SET is_public = ?, auction_name = ?, page_title = ?, page_description = ?,
                start_datetime = ?, end_datetime = ?, sale_mode = ?,
                updated_by = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (1 if is_public else 0, auction_name, page_title, page_description, start_datetime, end_datetime, sale_mode, current_user.id, auction_id))
        
        cur.execute("DELETE FROM proxy_service_users")
        for user_id in selected_users:
            cur.execute("INSERT OR IGNORE INTO proxy_service_users (user_id) VALUES (?)", (int(user_id),))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('オークション設定を更新しました', 'success')
    return redirect(url_for('admin_proxy_service_detail', auction_id=auction_id))

@app.route('/admin/proxy-service/<int:auction_id>/toggle-item/<int:item_id>', methods=['POST'])
@login_required
def admin_proxy_service_toggle_item(auction_id, item_id):
    """商品の代行サービス表示フラグを切り替え（特定オークション用）"""
    if not (current_user.is_owner() or current_user.is_admin()):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # 自分の商品かチェック（オーナーは全商品可）
        if current_user.is_owner():
            cur.execute("SELECT show_in_proxy_service, auction_id FROM merchandise WHERE id = %s", (item_id,))
        else:
            cur.execute("SELECT show_in_proxy_service, auction_id FROM merchandise WHERE id = %s AND user_id = %s", 
                       (item_id, current_user.id))
        item = cur.fetchone()
        
        if not item:
            return jsonify({'success': False, 'error': '商品が見つかりません'}), 404
        
        # トグル: このオークションに属していたら外す、属していなければ追加
        if item['auction_id'] == auction_id:
            cur.execute("UPDATE merchandise SET show_in_proxy_service = FALSE, auction_id = NULL WHERE id = %s", (item_id,))
            new_value = False
        else:
            cur.execute("UPDATE merchandise SET show_in_proxy_service = TRUE, auction_id = %s WHERE id = %s", (auction_id, item_id))
            new_value = True
    else:
        cur = conn.cursor()
        if current_user.is_owner():
            cur.execute("SELECT show_in_proxy_service, auction_id FROM merchandise WHERE id = ?", (item_id,))
        else:
            cur.execute("SELECT show_in_proxy_service, auction_id FROM merchandise WHERE id = ? AND user_id = ?", 
                       (item_id, current_user.id))
        item = cur.fetchone()
        
        if not item:
            return jsonify({'success': False, 'error': '商品が見つかりません'}), 404
        
        if item[1] == auction_id:
            cur.execute("UPDATE merchandise SET show_in_proxy_service = 0, auction_id = NULL WHERE id = ?", (item_id,))
            new_value = False
        else:
            cur.execute("UPDATE merchandise SET show_in_proxy_service = 1, auction_id = ? WHERE id = ?", (auction_id, item_id))
            new_value = True
    
    conn.commit()
    cur.close()
    conn.close()
    
    return jsonify({'success': True, 'new_value': new_value})

@app.route('/admin/proxy-service/<int:auction_id>/bulk-toggle', methods=['POST'])
@login_required
def admin_proxy_service_bulk_toggle(auction_id):
    """複数商品の代行サービス表示フラグを一括切り替え"""
    if not (current_user.is_owner() or current_user.is_admin()):
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    
    data = request.get_json()
    item_ids = data.get('item_ids', [])
    action = data.get('action', 'add')  # 'add' or 'remove'
    
    if not item_ids:
        return jsonify({'success': False, 'error': '商品が選択されていません'}), 400
    
    conn = get_db()
    updated_count = 0
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        for item_id in item_ids:
            try:
                if action == 'add':
                    if current_user.is_owner():
                        cur.execute("UPDATE merchandise SET show_in_proxy_service = TRUE, auction_id = %s WHERE id = %s", (auction_id, item_id))
                    else:
                        cur.execute("UPDATE merchandise SET show_in_proxy_service = TRUE, auction_id = %s WHERE id = %s AND user_id = %s", 
                                   (auction_id, item_id, current_user.id))
                else:
                    if current_user.is_owner():
                        cur.execute("UPDATE merchandise SET show_in_proxy_service = FALSE, auction_id = NULL WHERE id = %s", (item_id,))
                    else:
                        cur.execute("UPDATE merchandise SET show_in_proxy_service = FALSE, auction_id = NULL WHERE id = %s AND user_id = %s", 
                                   (item_id, current_user.id))
                updated_count += cur.rowcount
            except:
                pass
    else:
        cur = conn.cursor()
        for item_id in item_ids:
            try:
                if action == 'add':
                    if current_user.is_owner():
                        cur.execute("UPDATE merchandise SET show_in_proxy_service = 1, auction_id = ? WHERE id = ?", (auction_id, item_id))
                    else:
                        cur.execute("UPDATE merchandise SET show_in_proxy_service = 1, auction_id = ? WHERE id = ? AND user_id = ?", 
                                   (auction_id, item_id, current_user.id))
                else:
                    if current_user.is_owner():
                        cur.execute("UPDATE merchandise SET show_in_proxy_service = 0, auction_id = NULL WHERE id = ?", (item_id,))
                    else:
                        cur.execute("UPDATE merchandise SET show_in_proxy_service = 0, auction_id = NULL WHERE id = ? AND user_id = ?", 
                                   (item_id, current_user.id))
                updated_count += cur.rowcount
            except:
                pass
    
    conn.commit()
    cur.close()
    conn.close()
    
    return jsonify({'success': True, 'updated_count': updated_count})

@app.route('/admin/proxy-service/history')
@login_required
def admin_proxy_service_history():
    """代行サービス履歴（落札済み商品・入札履歴）"""
    if not (current_user.is_owner() or current_user.is_admin()):
        flash('オーナーまたは管理者権限が必要です', 'error')
        return redirect(url_for('index'))
    
    finalized_items = []
    bid_history = []
    
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            # 落札済み商品（代行サービス経由で売却された商品）
            # proxy_service_bidsに入札があり、sale_dateが入っている商品
            cur.execute("""
                SELECT m.id, m.product_name, m.brand_name, m.sale_date, m.sale_price,
                       m.photo_path, m.listing_price,
                       u.display_name as winner_name,
                       pb.bid_amount as winning_bid,
                       pb.created_at as bid_time
                FROM merchandise m
                JOIN proxy_service_bids pb ON m.id = pb.merchandise_id
                JOIN users u ON pb.user_id = u.id
                WHERE m.sale_date IS NOT NULL
                AND pb.bid_amount = (SELECT MAX(bid_amount) FROM proxy_service_bids WHERE merchandise_id = m.id)
                ORDER BY m.sale_date DESC
                LIMIT 100
            """)
            finalized_items = cur.fetchall()
            
            # 全入札履歴
            cur.execute("""
                SELECT pb.id, pb.bid_amount, pb.created_at, pb.bidder_name,
                       m.id as merchandise_id, m.product_name, m.brand_name, m.photo_path, m.sale_date,
                       u.display_name as user_display_name
                FROM proxy_service_bids pb
                JOIN merchandise m ON pb.merchandise_id = m.id
                LEFT JOIN users u ON pb.user_id = u.id
                ORDER BY pb.created_at DESC
                LIMIT 200
            """)
            bid_history = cur.fetchall()
        else:
            import sqlite3
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            # 落札済み商品
            cur.execute("""
                SELECT m.id, m.product_name, m.brand_name, m.sale_date, m.sale_price,
                       m.photo_path, m.listing_price,
                       u.display_name as winner_name,
                       pb.bid_amount as winning_bid,
                       pb.created_at as bid_time
                FROM merchandise m
                JOIN proxy_service_bids pb ON m.id = pb.merchandise_id
                JOIN users u ON pb.user_id = u.id
                WHERE m.sale_date IS NOT NULL
                AND pb.bid_amount = (SELECT MAX(bid_amount) FROM proxy_service_bids WHERE merchandise_id = m.id)
                ORDER BY m.sale_date DESC
                LIMIT 100
            """)
            finalized_items = [dict(row) for row in cur.fetchall()]
            
            # 全入札履歴
            cur.execute("""
                SELECT pb.id, pb.bid_amount, pb.created_at, pb.bidder_name,
                       m.id as merchandise_id, m.product_name, m.brand_name, m.photo_path, m.sale_date,
                       u.display_name as user_display_name
                FROM proxy_service_bids pb
                JOIN merchandise m ON pb.merchandise_id = m.id
                LEFT JOIN users u ON pb.user_id = u.id
                ORDER BY pb.created_at DESC
                LIMIT 200
            """)
            bid_history = [dict(row) for row in cur.fetchall()]
        
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Proxy service history error: {e}")
        import traceback
        traceback.print_exc()
    
    return render_template('admin/proxy_service_history.html',
                         finalized_items=finalized_items,
                         bid_history=bid_history)

@app.route('/admin/proxy-service/<int:auction_id>/finalize', methods=['POST'])
@login_required
def admin_proxy_service_finalize(auction_id):
    """オークション終了・落札確定処理（特定オークション用）"""
    if not (current_user.is_owner() or current_user.is_admin()):
        return jsonify({'success': False, 'error': 'オーナーまたは管理者権限が必要です'}), 403
    
    conn = get_db()
    finalized_count = 0
    today = datetime.now().strftime('%Y-%m-%d')
    now = datetime.now()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # このオークションで入札がある商品を取得（商品の全情報も取得）
        cur.execute("""
            SELECT m.*,
                   (SELECT user_id FROM proxy_service_bids WHERE merchandise_id = m.id ORDER BY bid_amount DESC LIMIT 1) as winner_user_id,
                   (SELECT bid_amount FROM proxy_service_bids WHERE merchandise_id = m.id ORDER BY bid_amount DESC LIMIT 1) as winning_bid,
                   (SELECT u.display_name FROM proxy_service_bids pb JOIN users u ON pb.user_id = u.id WHERE pb.merchandise_id = m.id ORDER BY pb.bid_amount DESC LIMIT 1) as winner_name
            FROM merchandise m
            WHERE m.auction_id = %s AND m.sale_date IS NULL
        """, (auction_id,))
        items = cur.fetchall()
        
        for item in items:
            if item['winner_user_id'] and item['winning_bid']:
                winner_user_id = item['winner_user_id']
                winning_bid = item['winning_bid']
                winner_name = item['winner_name'] or ''
                original_id = item['id']
                
                # 1. 管理者側の商品を「売却済み」にする
                cur.execute("""
                    UPDATE merchandise 
                    SET sale_date = %s,
                        sale_price = %s,
                        sale_type = 'auction',
                        show_in_proxy_service = FALSE
                    WHERE id = %s
                """, (today, winning_bid, original_id))
                
                # 2. 落札者用に新しい商品レコードを作成（仕入れ日=今日、未出品状態）
                cur.execute("""
                    INSERT INTO merchandise (
                        user_id, purchase_date, photo_path, product_name, brand_name,
                        item_condition, store_name, purchase_price, payment_method,
                        listing_price, expected_shipping, expected_commission,
                        model_number, supplier_detail, additional_photos, is_listed
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s
                    ) RETURNING id
                """, (
                    winner_user_id, today, item.get('photo_path'), item.get('product_name'), item.get('brand_name'),
                    item.get('item_condition'), '代行仕入れサービス', winning_bid, '代行仕入れ',
                    item.get('listing_price', 0), item.get('expected_shipping', 0), item.get('expected_commission', 0),
                    item.get('model_number'), '代行仕入れサービス', item.get('additional_photos'), False
                ))
                new_item_id = cur.fetchone()['id']
                
                # 3. 計算書を自動作成（管理者作成フラグ付き、送信待ち）
                doc_no = f"AUC-{now.strftime('%Y%m%d%H%M%S')}-{finalized_count + 1}"
                cur.execute("""
                    INSERT INTO user_keisan (
                        document_no, user_id, issue_date, recipient_name,
                        subject, total_amount, notes, status, is_admin_created
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, %s
                    ) RETURNING id
                """, (
                    doc_no, winner_user_id, today, winner_name,
                    '代行仕入れサービス落札', winning_bid,
                    f'商品名: {item.get("product_name", "商品")}\n商品ID: {original_id}', 'draft', True
                ))
                keisan_id = cur.fetchone()['id']
                
                # 計算書明細を追加
                cur.execute("""
                    INSERT INTO user_keisan_items (
                        keisan_id, item_no, item_name, quantity, unit_price, amount
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                """, (keisan_id, 1, item.get('product_name', '商品'), 1, winning_bid, winning_bid))
                
                # 4. 落札者の利用可能額を減算
                cur.execute("""
                    UPDATE users 
                    SET proxy_service_budget = GREATEST(0, COALESCE(proxy_service_budget, 0) - %s)
                    WHERE id = %s
                """, (winning_bid, winner_user_id))
                
                finalized_count += 1
        
        # このオークションを非公開に
        cur.execute("UPDATE proxy_service_settings SET is_public = FALSE WHERE id = %s", (auction_id,))
    else:
        cur = conn.cursor()
        
        # このオークションで入札がある商品を取得（商品の全情報も取得）
        cur.execute("""
            SELECT m.*,
                   (SELECT user_id FROM proxy_service_bids WHERE merchandise_id = m.id ORDER BY bid_amount DESC LIMIT 1) as winner_user_id,
                   (SELECT bid_amount FROM proxy_service_bids WHERE merchandise_id = m.id ORDER BY bid_amount DESC LIMIT 1) as winning_bid,
                   (SELECT u.display_name FROM proxy_service_bids pb JOIN users u ON pb.user_id = u.id WHERE pb.merchandise_id = m.id ORDER BY pb.bid_amount DESC LIMIT 1) as winner_name
            FROM merchandise m
            WHERE m.auction_id = ? AND m.sale_date IS NULL
        """, (auction_id,))
        items = cur.fetchall()
        
        for item in items:
            item_dict = dict(item)
            winner_user_id = item_dict.get('winner_user_id')
            winning_bid = item_dict.get('winning_bid')
            
            if winner_user_id and winning_bid:
                winner_name = item_dict.get('winner_name') or ''
                original_id = item_dict['id']
                
                # 1. 管理者側の商品を「売却済み」にする
                cur.execute("""
                    UPDATE merchandise 
                    SET sale_date = ?,
                        sale_price = ?,
                        sale_type = 'auction',
                        show_in_proxy_service = 0
                    WHERE id = ?
                """, (today, winning_bid, original_id))
                
                # 2. 落札者用に新しい商品レコードを作成（仕入れ日=今日、未出品状態）
                cur.execute("""
                    INSERT INTO merchandise (
                        user_id, purchase_date, photo_path, product_name, brand_name,
                        item_condition, store_name, purchase_price, payment_method,
                        listing_price, expected_shipping, expected_commission,
                        model_number, supplier_detail, additional_photos, is_listed
                    ) VALUES (
                        ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?
                    )
                """, (
                    winner_user_id, today, item_dict.get('photo_path'), item_dict.get('product_name'), item_dict.get('brand_name'),
                    item_dict.get('item_condition'), '代行仕入れサービス', winning_bid, '代行仕入れ',
                    item_dict.get('listing_price') or 0, item_dict.get('expected_shipping') or 0, item_dict.get('expected_commission') or 0,
                    item_dict.get('model_number'), '代行仕入れサービス', item_dict.get('additional_photos'), 0
                ))
                new_item_id = cur.lastrowid
                
                # 3. 計算書を自動作成（管理者作成フラグ付き、送信待ち）
                doc_no = f"AUC-{now.strftime('%Y%m%d%H%M%S')}-{finalized_count + 1}"
                cur.execute("""
                    INSERT INTO user_keisan (
                        document_no, user_id, issue_date, recipient_name,
                        subject, total_amount, notes, status, is_admin_created
                    ) VALUES (
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                """, (
                    doc_no, winner_user_id, today, winner_name,
                    '代行仕入れサービス落札', winning_bid,
                    f'商品名: {item_dict.get("product_name", "商品")}\n商品ID: {original_id}', 'draft', 1
                ))
                keisan_id = cur.lastrowid
                
                # 計算書明細を追加
                cur.execute("""
                    INSERT INTO user_keisan_items (
                        keisan_id, item_no, item_name, quantity, unit_price, amount
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """, (keisan_id, 1, item_dict.get('product_name', '商品'), 1, winning_bid, winning_bid))
                
                # 4. 落札者の利用可能額を減算
                cur.execute("""
                    UPDATE users 
                    SET proxy_service_budget = MAX(0, COALESCE(proxy_service_budget, 0) - ?)
                    WHERE id = ?
                """, (winning_bid, winner_user_id))
                
                finalized_count += 1
        
        # このオークションを非公開に
        cur.execute("UPDATE proxy_service_settings SET is_public = 0 WHERE id = ?", (auction_id,))
    
    conn.commit()
    cur.close()
    conn.close()
    
    return jsonify({
        'success': True, 
        'message': f'{finalized_count}件の商品を落札者に割り当てました。計算書を送信待ちに追加しました。',
        'finalized_count': finalized_count
    })

@app.route('/admin/auction-keisan')
@login_required
@admin_required
def admin_auction_keisan_list():
    """管理者用：オークション落札計算書一覧"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT k.*, u.display_name as user_name, u.username
            FROM user_keisan k
            JOIN users u ON k.user_id = u.id
            WHERE k.is_admin_created = TRUE
            ORDER BY k.created_at DESC
        """)
        keisan_list = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT k.*, u.display_name as user_name, u.username
            FROM user_keisan k
            JOIN users u ON k.user_id = u.id
            WHERE k.is_admin_created = 1
            ORDER BY k.created_at DESC
        """)
        keisan_list = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return render_template('admin/auction_keisan_list.html', keisan_list=keisan_list)

@app.route('/admin/auction-keisan/<int:id>')
@login_required
@admin_required
def admin_auction_keisan_view(id):
    """管理者用：オークション落札計算書詳細"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT k.*, u.display_name as user_name, u.username, u.email
            FROM user_keisan k
            JOIN users u ON k.user_id = u.id
            WHERE k.id = %s AND k.is_admin_created = TRUE
        """, (id,))
        keisan = cur.fetchone()
        if keisan:
            keisan = dict(keisan)
            cur.execute("SELECT * FROM user_keisan_items WHERE keisan_id = %s ORDER BY item_no", (id,))
            items = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT k.*, u.display_name as user_name, u.username, u.email
            FROM user_keisan k
            JOIN users u ON k.user_id = u.id
            WHERE k.id = ? AND k.is_admin_created = 1
        """, (id,))
        keisan = cur.fetchone()
        if keisan:
            keisan = dict(keisan)
            cur.execute("SELECT * FROM user_keisan_items WHERE keisan_id = ? ORDER BY item_no", (id,))
            items = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    if not keisan:
        flash('計算書が見つかりません', 'error')
        return redirect(url_for('admin_auction_keisan_list'))
    
    return render_template('admin/auction_keisan_view.html', keisan=keisan, items=items)

@app.route('/admin/auction-keisan/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_auction_keisan_edit(id):
    """管理者用：オークション落札計算書編集"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT k.*, u.display_name as user_name
            FROM user_keisan k
            JOIN users u ON k.user_id = u.id
            WHERE k.id = %s AND k.is_admin_created = TRUE
        """, (id,))
        keisan = cur.fetchone()
        if keisan:
            keisan = dict(keisan)
            cur.execute("SELECT * FROM user_keisan_items WHERE keisan_id = %s ORDER BY item_no", (id,))
            items = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT k.*, u.display_name as user_name
            FROM user_keisan k
            JOIN users u ON k.user_id = u.id
            WHERE k.id = ? AND k.is_admin_created = 1
        """, (id,))
        keisan = cur.fetchone()
        if keisan:
            keisan = dict(keisan)
            cur.execute("SELECT * FROM user_keisan_items WHERE keisan_id = ? ORDER BY item_no", (id,))
            items = [dict(row) for row in cur.fetchall()]
    
    if not keisan:
        flash('計算書が見つかりません', 'error')
        return redirect(url_for('admin_auction_keisan_list'))
    
    if request.method == 'POST':
        issue_date = request.form.get('issue_date')
        recipient_name = request.form.get('recipient_name', '')
        subject = request.form.get('subject', '')
        notes = request.form.get('notes', '')
        
        # 明細を取得
        item_names = request.form.getlist('item_name[]')
        quantities = request.form.getlist('quantity[]')
        units = request.form.getlist('unit[]')
        unit_prices = request.form.getlist('unit_price[]')
        
        total_amount = 0
        new_items = []
        for i, (name, qty, unit, price) in enumerate(zip(item_names, quantities, units, unit_prices), start=1):
            if name.strip():
                qty = int(qty) if qty else 1
                price = int(price) if price else 0
                amount = qty * price
                total_amount += amount
                new_items.append((i, name, qty, unit, price, amount))
        
        if DATABASE_URL:
            cur.execute("""
                UPDATE user_keisan SET
                    issue_date = %s, recipient_name = %s, subject = %s,
                    notes = %s, total_amount = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (issue_date, recipient_name, subject, notes, total_amount, id))
            
            cur.execute("DELETE FROM user_keisan_items WHERE keisan_id = %s", (id,))
            for item in new_items:
                cur.execute("""
                    INSERT INTO user_keisan_items (keisan_id, item_no, item_name, quantity, unit, unit_price, amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (id, *item))
        else:
            cur.execute("""
                UPDATE user_keisan SET
                    issue_date = ?, recipient_name = ?, subject = ?,
                    notes = ?, total_amount = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (issue_date, recipient_name, subject, notes, total_amount, id))
            
            cur.execute("DELETE FROM user_keisan_items WHERE keisan_id = ?", (id,))
            for item in new_items:
                cur.execute("""
                    INSERT INTO user_keisan_items (keisan_id, item_no, item_name, quantity, unit, unit_price, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (id, *item))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('計算書を更新しました', 'success')
        return redirect(url_for('admin_auction_keisan_view', id=id))
    
    cur.close()
    conn.close()
    
    return render_template('admin/auction_keisan_edit.html', keisan=keisan, items=items)

@app.route('/admin/auction-keisan/<int:id>/submit', methods=['POST'])
@login_required
@admin_required
def admin_auction_keisan_submit(id):
    """管理者用：計算書をユーザーに送信（ステータスを submitted に変更）"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            UPDATE user_keisan SET status = 'submitted', updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND is_admin_created = TRUE
        """, (id,))
    else:
        cur = conn.cursor()
        cur.execute("""
            UPDATE user_keisan SET status = 'submitted', updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND is_admin_created = 1
        """, (id,))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('計算書をユーザーに送信しました', 'success')
    return redirect(url_for('admin_auction_keisan_list'))

@app.route('/admin/auction-keisan/<int:id>/pdf')
@login_required
@admin_required
def admin_auction_keisan_pdf(id):
    """管理者用：オークション落札計算書PDF"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT k.*, u.display_name as user_name
            FROM user_keisan k
            JOIN users u ON k.user_id = u.id
            WHERE k.id = %s AND k.is_admin_created = TRUE
        """, (id,))
        keisan = cur.fetchone()
        if keisan:
            keisan = dict(keisan)
            cur.execute("SELECT * FROM user_keisan_items WHERE keisan_id = %s ORDER BY item_no", (id,))
            items = [dict(row) for row in cur.fetchall()]
        else:
            items = []
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT k.*, u.display_name as user_name
            FROM user_keisan k
            JOIN users u ON k.user_id = u.id
            WHERE k.id = ? AND k.is_admin_created = 1
        """, (id,))
        keisan = cur.fetchone()
        if keisan:
            keisan = dict(keisan)
            cur.execute("SELECT * FROM user_keisan_items WHERE keisan_id = ? ORDER BY item_no", (id,))
            items = [dict(row) for row in cur.fetchall()]
        else:
            items = []
    
    cur.close()
    conn.close()
    
    if not keisan:
        flash('計算書が見つかりません', 'error')
        return redirect(url_for('admin_auction_keisan_list'))
    
    return render_template('pdf/keisan.html', keisan=keisan, items=items)

@app.route('/proxy-service')
def public_proxy_service_list():
    """代行仕入れサービス公開ページ - オークション一覧"""
    conn = get_db()
    now = datetime.now()
    
    # サービス利用対象ユーザーかチェック（ログイン時のみ）
    if current_user.is_authenticated:
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM proxy_service_users WHERE user_id = %s", (current_user.id,))
            is_allowed_user = cur.fetchone() is not None
            cur.close()
        else:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM proxy_service_users WHERE user_id = ?", (current_user.id,))
            is_allowed_user = cur.fetchone() is not None
            cur.close()
        
        if not is_allowed_user and not current_user.is_admin() and not current_user.is_owner():
            conn.close()
            return render_template('proxy_service_closed.html', reason='not_allowed')
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # 公開中のオークション一覧を取得
        cur.execute("""
            SELECT ps.*, 
                   (SELECT COUNT(*) FROM merchandise m WHERE m.auction_id = ps.id AND m.sale_date IS NULL) as item_count
            FROM proxy_service_settings ps
            WHERE ps.is_public = TRUE
            ORDER BY ps.id DESC
        """)
        auctions = [dict(a) for a in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT ps.*, 
                   (SELECT COUNT(*) FROM merchandise m WHERE m.auction_id = ps.id AND m.sale_date IS NULL) as item_count
            FROM proxy_service_settings ps
            WHERE ps.is_public = 1
            ORDER BY ps.id DESC
        """)
        columns = ['id', 'is_public', 'page_title', 'page_description', 'start_datetime', 'end_datetime', 'updated_by', 'updated_at', 'sale_mode', 'auction_name', 'item_count']
        auctions = []
        for row in cur.fetchall():
            a = dict(zip(columns[:len(row)], row))
            auctions.append(a)
    
    # 有効なオークションのみフィルタリング（日時チェック）
    active_auctions = []
    for auction in auctions:
        start_dt = auction.get('start_datetime')
        end_dt = auction.get('end_datetime')
        
        if start_dt:
            if isinstance(start_dt, str):
                start_dt = datetime.fromisoformat(start_dt)
            if now < start_dt:
                continue
        
        if end_dt:
            if isinstance(end_dt, str):
                end_dt = datetime.fromisoformat(end_dt)
            if now > end_dt:
                continue
        
        active_auctions.append(auction)
    
    cur.close()
    conn.close()
    
    # オークションが1つだけなら直接そのページへリダイレクト
    if len(active_auctions) == 1:
        return redirect(url_for('public_proxy_service', auction_id=active_auctions[0]['id']))
    
    # オークションがなければ閉鎖ページ
    if not active_auctions:
        return render_template('proxy_service_closed.html', reason='disabled')
    
    return render_template('proxy_service_list.html', auctions=active_auctions)

@app.route('/proxy-service/<int:auction_id>')
def public_proxy_service(auction_id):
    """代行仕入れサービス公開ページ（個別オークション）"""
    conn = get_db()
    now = datetime.now()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM proxy_service_settings WHERE id = %s", (auction_id,))
        settings = cur.fetchone()
        
        if not settings or not settings['is_public']:
            return render_template('proxy_service_closed.html', reason='disabled')
        
        settings_dict = dict(settings)
        
        # サービス利用対象ユーザーかチェック（ログイン時のみ）
        if current_user.is_authenticated:
            cur.execute("SELECT 1 FROM proxy_service_users WHERE user_id = %s", (current_user.id,))
            is_allowed_user = cur.fetchone() is not None
            # 管理者/オーナーは常にアクセス可能
            if not is_allowed_user and not current_user.is_admin() and not current_user.is_owner():
                return render_template('proxy_service_closed.html', reason='not_allowed')
        
        # 日時チェック
        start_dt = settings_dict.get('start_datetime')
        end_dt = settings_dict.get('end_datetime')
        
        if start_dt and now < start_dt:
            return render_template('proxy_service_closed.html', reason='not_started', start_datetime=start_dt)
        
        if end_dt and now > end_dt:
            return render_template('proxy_service_closed.html', reason='ended', end_datetime=end_dt)
        
        # このオークションに掲載中の商品を取得
        cur.execute("""
            SELECT m.id, m.photo_path, m.additional_photos, m.product_name, m.brand_name, m.item_condition,
                   m.listing_price, m.model_number, COALESCE(u.display_name, '不明') as owner_name,
                   (SELECT bid_amount FROM proxy_service_bids WHERE merchandise_id = m.id ORDER BY bid_amount DESC LIMIT 1) as highest_bid,
                   (SELECT bidder_name FROM proxy_service_bids WHERE merchandise_id = m.id ORDER BY bid_amount DESC LIMIT 1) as highest_bidder
            FROM merchandise m
            LEFT JOIN users u ON m.user_id = u.id
            WHERE m.auction_id = %s AND m.sale_date IS NULL
            ORDER BY m.id DESC
        """, (auction_id,))
        items = cur.fetchall()
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM proxy_service_settings WHERE id = ?", (auction_id,))
        row = cur.fetchone()
        
        if not row or not row[1]:  # is_public
            return render_template('proxy_service_closed.html', reason='disabled')
        
        # SQLiteの場合、カラム名でアクセスできるようにする
        columns = ['id', 'is_public', 'page_title', 'page_description', 'start_datetime', 'end_datetime', 'updated_by', 'updated_at', 'sale_mode', 'auction_name']
        settings_dict = dict(zip(columns, row[:len(columns)]))
        # sale_modeが取得できない場合のデフォルト値
        if 'sale_mode' not in settings_dict or settings_dict.get('sale_mode') is None:
            settings_dict['sale_mode'] = 'auction'
        
        # サービス利用対象ユーザーかチェック（ログイン時のみ）
        if current_user.is_authenticated:
            cur.execute("SELECT 1 FROM proxy_service_users WHERE user_id = ?", (current_user.id,))
            is_allowed_user = cur.fetchone() is not None
            # 管理者/オーナーは常にアクセス可能
            if not is_allowed_user and not current_user.is_admin() and not current_user.is_owner():
                return render_template('proxy_service_closed.html', reason='not_allowed')
        
        # 日時チェック
        start_dt = settings_dict.get('start_datetime')
        end_dt = settings_dict.get('end_datetime')
        
        if start_dt:
            start_dt = datetime.fromisoformat(start_dt) if isinstance(start_dt, str) else start_dt
            if now < start_dt:
                return render_template('proxy_service_closed.html', reason='not_started', start_datetime=start_dt)
        
        if end_dt:
            end_dt = datetime.fromisoformat(end_dt) if isinstance(end_dt, str) else end_dt
            if now > end_dt:
                return render_template('proxy_service_closed.html', reason='ended', end_datetime=end_dt)
        
        # このオークションに掲載中の商品を取得
        cur.execute("""
            SELECT m.id, m.photo_path, m.additional_photos, m.product_name, m.brand_name, m.item_condition,
                   m.listing_price, m.model_number, COALESCE(u.display_name, '不明') as owner_name,
                   (SELECT bid_amount FROM proxy_service_bids WHERE merchandise_id = m.id ORDER BY bid_amount DESC LIMIT 1) as highest_bid,
                   (SELECT bidder_name FROM proxy_service_bids WHERE merchandise_id = m.id ORDER BY bid_amount DESC LIMIT 1) as highest_bidder
            FROM merchandise m
            LEFT JOIN users u ON m.user_id = u.id
            WHERE m.auction_id = ? AND m.sale_date IS NULL
            ORDER BY m.id DESC
        """, (auction_id,))
        items = cur.fetchall()
    
    cur.close()
    conn.close()
    
    return render_template('proxy_service_public.html',
                         settings=settings_dict,
                         items=[dict(i) for i in items],
                         end_datetime=end_dt if 'end_dt' in dir() else settings_dict.get('end_datetime'),
                         auction_id=auction_id)

@app.route('/proxy-service/bid', methods=['POST'])
def proxy_service_bid():
    """入札API（ログインユーザー専用）"""
    # ログインチェック
    if not current_user.is_authenticated:
        return jsonify({'success': False, 'error': 'ログインが必要です'}), 401
    
    # 支払い遅延チェック
    if not current_user.can_participate_auction():
        return jsonify({'success': False, 'error': '月謝のお支払いが確認できていないため、入札できません'}), 403
    
    # サービス利用対象ユーザーかチェック（管理者/オーナーは除外）
    if not current_user.is_admin() and not current_user.is_owner():
        conn_check = get_db()
        if DATABASE_URL:
            cur_check = conn_check.cursor()
            cur_check.execute("SELECT 1 FROM proxy_service_users WHERE user_id = %s", (current_user.id,))
        else:
            cur_check = conn_check.cursor()
            cur_check.execute("SELECT 1 FROM proxy_service_users WHERE user_id = ?", (current_user.id,))
        is_allowed = cur_check.fetchone() is not None
        cur_check.close()
        conn_check.close()
        if not is_allowed:
            return jsonify({'success': False, 'error': 'このサービスを利用する権限がありません'}), 403
    
    data = request.get_json() or request.form
    
    merchandise_id = data.get('merchandise_id')
    bid_amount = data.get('bid_amount')
    
    # ログインユーザーの情報を使用
    user_id = current_user.id
    bidder_name = current_user.display_name or current_user.username
    
    if not merchandise_id or not bid_amount:
        return jsonify({'success': False, 'error': '必須項目が入力されていません'}), 400
    
    try:
        bid_amount = int(bid_amount)
        if bid_amount <= 0:
            return jsonify({'success': False, 'error': '有効な金額を入力してください'}), 400
    except ValueError:
        return jsonify({'success': False, 'error': '有効な金額を入力してください'}), 400
    
    conn = get_db()
    now = datetime.now()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # まず商品を取得してauction_idを確認
        cur.execute("SELECT id, listing_price, auction_id FROM merchandise WHERE id = %s AND show_in_proxy_service = TRUE", (merchandise_id,))
        item = cur.fetchone()
        if not item:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': '商品が見つかりません'}), 404
        
        # 商品のauction_idに基づいて設定を取得
        auction_id = item.get('auction_id')
        if auction_id:
            cur.execute("SELECT * FROM proxy_service_settings WHERE id = %s", (auction_id,))
        else:
            cur.execute("SELECT * FROM proxy_service_settings LIMIT 1")
        settings = cur.fetchone()
        
        if not settings or not settings['is_public']:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'オークションは現在開催されていません'}), 400
        
        start_dt = settings.get('start_datetime')
        end_dt = settings.get('end_datetime')
        
        if start_dt and now < start_dt:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'オークションはまだ開始されていません'}), 400
        if end_dt and now > end_dt:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'オークションは終了しました'}), 400
        
        # 現在の最高入札を確認
        cur.execute("SELECT bid_amount FROM proxy_service_bids WHERE merchandise_id = %s ORDER BY bid_amount DESC LIMIT 1", (merchandise_id,))
        highest = cur.fetchone()
        min_bid = highest['bid_amount'] + 1 if highest else (item['listing_price'] or 0)
        
        if bid_amount < min_bid:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': f'入札額は¥{min_bid:,}以上にしてください'}), 400
        
        # 利用可能金額チェック
        if not current_user.can_purchase_proxy_item(bid_amount):
            remaining = current_user.get_proxy_service_remaining_budget()
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': f'利用可能残高を超えています（残高: ¥{remaining:,}）'}), 400
        
        # 入札を保存（user_id含む）
        cur.execute("INSERT INTO proxy_service_bids (merchandise_id, user_id, bidder_name, bid_amount) VALUES (%s, %s, %s, %s)",
                   (merchandise_id, user_id, bidder_name, bid_amount))
    else:
        cur = conn.cursor()
        
        # まず商品を取得してauction_idを確認
        cur.execute("SELECT id, listing_price, auction_id FROM merchandise WHERE id = ? AND show_in_proxy_service = 1", (merchandise_id,))
        item = cur.fetchone()
        if not item:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': '商品が見つかりません'}), 404
        
        # 商品のauction_idに基づいて設定を取得
        auction_id = item[2] if len(item) > 2 else None  # auction_id
        if auction_id:
            cur.execute("SELECT * FROM proxy_service_settings WHERE id = ?", (auction_id,))
        else:
            cur.execute("SELECT * FROM proxy_service_settings LIMIT 1")
        row = cur.fetchone()
        
        if not row or not row[1]:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'オークションは現在開催されていません'}), 400
        
        # 時間チェック（SQLiteの場合）
        start_dt_idx = 5  # start_datetime
        end_dt_idx = 6    # end_datetime
        if len(row) > start_dt_idx and row[start_dt_idx]:
            start_dt = row[start_dt_idx]
            if isinstance(start_dt, str):
                try:
                    start_dt = datetime.fromisoformat(start_dt)
                except:
                    start_dt = None
            if start_dt and now < start_dt:
                cur.close()
                conn.close()
                return jsonify({'success': False, 'error': 'オークションはまだ開始されていません'}), 400
        if len(row) > end_dt_idx and row[end_dt_idx]:
            end_dt = row[end_dt_idx]
            if isinstance(end_dt, str):
                try:
                    end_dt = datetime.fromisoformat(end_dt)
                except:
                    end_dt = None
            if end_dt and now > end_dt:
                cur.close()
                conn.close()
                return jsonify({'success': False, 'error': 'オークションは終了しました'}), 400
        
        # 現在の最高入札を確認
        cur.execute("SELECT bid_amount FROM proxy_service_bids WHERE merchandise_id = ? ORDER BY bid_amount DESC LIMIT 1", (merchandise_id,))
        highest = cur.fetchone()
        min_bid = highest[0] + 1 if highest else (item[1] or 0)
        
        if bid_amount < min_bid:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': f'入札額は¥{min_bid:,}以上にしてください'}), 400
        
        # 利用可能金額チェック
        if not current_user.can_purchase_proxy_item(bid_amount):
            remaining = current_user.get_proxy_service_remaining_budget()
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': f'利用可能残高を超えています（残高: ¥{remaining:,}）'}), 400
        
        # 入札を保存（user_id含む）
        cur.execute("INSERT INTO proxy_service_bids (merchandise_id, user_id, bidder_name, bid_amount) VALUES (?, ?, ?, ?)",
                   (merchandise_id, user_id, bidder_name, bid_amount))
    
    conn.commit()
    cur.close()
    conn.close()
    
    return jsonify({
        'success': True, 
        'message': '入札が完了しました',
        'bid_amount': bid_amount,
        'bidder_name': bidder_name
    })

@app.route('/proxy-service/purchase', methods=['POST'])
def proxy_service_purchase():
    """即決購入API（早い者勝ちモード用）"""
    # ログインチェック
    if not current_user.is_authenticated:
        return jsonify({'success': False, 'error': 'ログインが必要です'}), 401
    
    # 支払い遅延チェック
    if not current_user.can_participate_auction():
        return jsonify({'success': False, 'error': '月謝のお支払いが確認できていないため、購入できません'}), 403
    
    data = request.get_json() or request.form
    merchandise_id = data.get('merchandise_id')
    
    if not merchandise_id:
        return jsonify({'success': False, 'error': '商品IDが指定されていません'}), 400
    
    user_id = current_user.id
    buyer_name = current_user.display_name or current_user.username
    
    conn = get_db()
    now = datetime.now()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # まず商品を取得してauction_idを確認
        cur.execute("""
            SELECT id, listing_price, product_name, sale_date, auction_id 
            FROM merchandise 
            WHERE id = %s AND show_in_proxy_service = TRUE
        """, (merchandise_id,))
        item = cur.fetchone()
        if not item:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': '商品が見つかりません'}), 404
        
        # 既に購入済みかチェック
        if item.get('sale_date'):
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'この商品は既に購入済みです'}), 400
        
        # 商品のauction_idに基づいて設定を取得
        auction_id = item.get('auction_id')
        if auction_id:
            cur.execute("SELECT * FROM proxy_service_settings WHERE id = %s", (auction_id,))
        else:
            cur.execute("SELECT * FROM proxy_service_settings LIMIT 1")
        settings = cur.fetchone()
        
        if not settings or not settings['is_public']:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': '販売は現在行われていません'}), 400
        
        # 即決モードか確認
        if settings.get('sale_mode') != 'fixed':
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'この商品は即決購入できません（このオークションは入札形式です）'}), 400
        
        start_dt = settings.get('start_datetime')
        end_dt = settings.get('end_datetime')
        
        if start_dt and now < start_dt:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': '販売はまだ開始されていません'}), 400
        if end_dt and now > end_dt:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': '販売は終了しました'}), 400
        
        purchase_price = item['listing_price'] or 0
        
        # 利用可能金額チェック
        if not current_user.can_purchase_proxy_item(purchase_price):
            remaining = current_user.get_proxy_service_remaining_budget()
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': f'利用可能残高が不足しています（残高: ¥{remaining:,}）'}), 400
        
        # 商品を購入済みにマーク（sale_dateを設定、購入者情報を記録）
        cur.execute("""
            UPDATE merchandise 
            SET sale_date = %s, 
                sale_price = %s,
                sales_destination = %s,
                show_in_proxy_service = FALSE
            WHERE id = %s AND sale_date IS NULL
        """, (now.date(), purchase_price, f'即決購入: {buyer_name}', merchandise_id))
        
        if cur.rowcount == 0:
            conn.rollback()
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'この商品は既に購入されました'}), 400
        
        # 入札履歴にも記録（購入記録として）
        cur.execute("INSERT INTO proxy_service_bids (merchandise_id, user_id, bidder_name, bid_amount) VALUES (%s, %s, %s, %s)",
                   (merchandise_id, user_id, f'{buyer_name}（購入）', purchase_price))
    else:
        cur = conn.cursor()
        
        # まず商品を取得してauction_idを確認
        cur.execute("""
            SELECT id, listing_price, product_name, sale_date, auction_id 
            FROM merchandise 
            WHERE id = ? AND show_in_proxy_service = 1
        """, (merchandise_id,))
        item = cur.fetchone()
        if not item:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': '商品が見つかりません'}), 404
        
        # 既に購入済みかチェック
        if item[3]:  # sale_date
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'この商品は既に購入済みです'}), 400
        
        # 商品のauction_idに基づいて設定を取得
        auction_id = item[4] if len(item) > 4 else None  # auction_id
        if auction_id:
            cur.execute("SELECT * FROM proxy_service_settings WHERE id = ?", (auction_id,))
        else:
            cur.execute("SELECT * FROM proxy_service_settings LIMIT 1")
        row = cur.fetchone()
        
        if not row or not row[1]:  # is_public
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': '販売は現在行われていません'}), 400
        
        # sale_modeの位置を確認（8番目のカラム、インデックス8）
        sale_mode = row[8] if len(row) > 8 else 'auction'
        if sale_mode != 'fixed':
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'この商品は即決購入できません（このオークションは入札形式です）'}), 400
        
        purchase_price = item[1] or 0
        
        # 利用可能金額チェック
        if not current_user.can_purchase_proxy_item(purchase_price):
            remaining = current_user.get_proxy_service_remaining_budget()
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': f'利用可能残高が不足しています（残高: ¥{remaining:,}）'}), 400
        
        # 商品を購入済みにマーク
        cur.execute("""
            UPDATE merchandise 
            SET sale_date = ?, 
                sale_price = ?,
                sales_destination = ?,
                show_in_proxy_service = 0
            WHERE id = ? AND sale_date IS NULL
        """, (now.strftime('%Y-%m-%d'), purchase_price, f'即決購入: {buyer_name}', merchandise_id))
        
        if cur.rowcount == 0:
            conn.rollback()
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': 'この商品は既に購入されました'}), 400
        
        # 入札履歴にも記録
        cur.execute("INSERT INTO proxy_service_bids (merchandise_id, user_id, bidder_name, bid_amount) VALUES (?, ?, ?, ?)",
                   (merchandise_id, user_id, f'{buyer_name}（購入）', purchase_price))
    
    conn.commit()
    cur.close()
    conn.close()
    
    return jsonify({
        'success': True,
        'message': '購入が完了しました！',
        'purchase_price': purchase_price,
        'buyer_name': buyer_name
    })

# ===================
# マスター設定
# ===================

@app.route('/admin/master-settings')
@login_required
@admin_required
def admin_master_settings():
    """マスター設定画面（開花管理用 - scope='admin'）"""
    conn = get_db()
    scope = 'admin'
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # ブランドカテゴリ（scope指定）
        cur.execute("SELECT * FROM master_brand_categories WHERE scope = %s OR scope IS NULL ORDER BY display_order, id", (scope,))
        brand_categories = [dict(row) for row in cur.fetchall()]
        
        # ブランド
        cur.execute("SELECT * FROM master_brands WHERE scope = %s OR scope IS NULL ORDER BY display_order, id", (scope,))
        brands = [dict(row) for row in cur.fetchall()]
        
        # 仕入先
        cur.execute("SELECT * FROM master_suppliers WHERE scope = %s OR scope IS NULL ORDER BY display_order, id", (scope,))
        suppliers = [dict(row) for row in cur.fetchall()]
        
        # 商品状態
        cur.execute("SELECT * FROM master_conditions WHERE scope = %s OR scope IS NULL ORDER BY display_order, id", (scope,))
        conditions = [dict(row) for row in cur.fetchall()]
        
        # 支払方法
        cur.execute("SELECT * FROM master_payment_methods WHERE scope = %s OR scope IS NULL ORDER BY display_order, id", (scope,))
        payment_methods = [dict(row) for row in cur.fetchall()]
        
        # 仕入先詳細
        cur.execute("SELECT * FROM master_supplier_details WHERE scope = %s OR scope IS NULL ORDER BY display_order, id", (scope,))
        supplier_details = [dict(row) for row in cur.fetchall()]
        
        # 書類設定
        cur.execute("SELECT * FROM master_document_settings ORDER BY category, display_order, id")
        document_settings_raw = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        
        cur.execute("SELECT * FROM master_brand_categories WHERE scope = ? OR scope IS NULL ORDER BY display_order, id", (scope,))
        brand_categories = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_brands WHERE scope = ? OR scope IS NULL ORDER BY display_order, id", (scope,))
        brands = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_suppliers WHERE scope = ? OR scope IS NULL ORDER BY display_order, id", (scope,))
        suppliers = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_conditions WHERE scope = ? OR scope IS NULL ORDER BY display_order, id", (scope,))
        conditions = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_payment_methods WHERE scope = ? OR scope IS NULL ORDER BY display_order, id", (scope,))
        payment_methods = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_supplier_details WHERE scope = ? OR scope IS NULL ORDER BY display_order, id", (scope,))
        supplier_details = [dict(row) for row in cur.fetchall()]
        
        # 書類設定
        cur.execute("SELECT * FROM master_document_settings ORDER BY category, display_order, id")
        document_settings_raw = [dict(row) for row in cur.fetchall()]
    
    # 書類設定をキーで辞書化
    document_settings = {row['setting_key']: row['setting_value'] for row in document_settings_raw}
    
    cur.close()
    conn.close()
    
    return render_template('admin/master_settings.html',
                         brand_categories=brand_categories,
                         brands=brands,
                         suppliers=suppliers,
                         conditions=conditions,
                         payment_methods=payment_methods,
                         supplier_details=supplier_details,
                         document_settings=document_settings,
                         scope=scope)


@app.route('/user/master-settings')
@login_required
def user_master_settings():
    """ユーザー機能用マスター設定画面（scope='user'）"""
    conn = get_db()
    scope = 'user'
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # ブランドカテゴリ
        cur.execute("SELECT * FROM master_brand_categories WHERE scope = %s ORDER BY display_order, id", (scope,))
        brand_categories = [dict(row) for row in cur.fetchall()]
        
        # ブランド
        cur.execute("SELECT * FROM master_brands WHERE scope = %s ORDER BY display_order, id", (scope,))
        brands = [dict(row) for row in cur.fetchall()]
        
        # 仕入先
        cur.execute("SELECT * FROM master_suppliers WHERE scope = %s ORDER BY display_order, id", (scope,))
        suppliers = [dict(row) for row in cur.fetchall()]
        
        # 商品状態
        cur.execute("SELECT * FROM master_conditions WHERE scope = %s ORDER BY display_order, id", (scope,))
        conditions = [dict(row) for row in cur.fetchall()]
        
        # 支払方法
        cur.execute("SELECT * FROM master_payment_methods WHERE scope = %s ORDER BY display_order, id", (scope,))
        payment_methods = [dict(row) for row in cur.fetchall()]
        
        # 仕入先詳細
        cur.execute("SELECT * FROM master_supplier_details WHERE scope = %s ORDER BY display_order, id", (scope,))
        supplier_details = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        
        cur.execute("SELECT * FROM master_brand_categories WHERE scope = ? ORDER BY display_order, id", (scope,))
        brand_categories = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_brands WHERE scope = ? ORDER BY display_order, id", (scope,))
        brands = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_suppliers WHERE scope = ? ORDER BY display_order, id", (scope,))
        suppliers = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_conditions WHERE scope = ? ORDER BY display_order, id", (scope,))
        conditions = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_payment_methods WHERE scope = ? ORDER BY display_order, id", (scope,))
        payment_methods = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_supplier_details WHERE scope = ? ORDER BY display_order, id", (scope,))
        supplier_details = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return render_template('user_master_settings.html',
                         brand_categories=brand_categories,
                         brands=brands,
                         suppliers=suppliers,
                         conditions=conditions,
                         payment_methods=payment_methods,
                         supplier_details=supplier_details,
                         scope=scope)


@app.route('/admin/master-settings/init', methods=['POST'])
@login_required
@admin_required
def admin_master_settings_init():
    """マスターデータの初期登録"""
    conn = get_db()
    
    # デフォルトデータ
    default_brand_categories = [
        ('ラグジュアリーブランド', 1),
        ('時計・ジュエリー', 2),
        ('スポーツ・ストリート', 3),
        ('日本ブランド', 4),
        ('電子機器', 5),
        ('その他', 6)
    ]
    
    default_brands = [
        # ラグジュアリーブランド
        (1, 'Louis Vuitton', 'Louis Vuitton（ルイ・ヴィトン）', 'ヴィトン,ビトン,LV,ルイヴィトン', 1),
        (1, 'Hermes', 'Hermes（エルメス）', 'エルメス,HERMES,バーキン,ケリー', 2),
        (1, 'Chanel', 'Chanel（シャネル）', 'シャネル,CHANEL,マトラッセ', 3),
        (1, 'Gucci', 'Gucci（グッチ）', 'グッチ,GUCCI', 4),
        (1, 'Prada', 'Prada（プラダ）', 'プラダ,PRADA', 5),
        (1, 'Dior', 'Dior（ディオール）', 'ディオール,DIOR', 6),
        (1, 'Celine', 'Celine（セリーヌ）', 'セリーヌ,CELINE', 7),
        (1, 'Bottega Veneta', 'Bottega Veneta（ボッテガ・ヴェネタ）', 'ボッテガ,BOTTEGA', 8),
        (1, 'Balenciaga', 'Balenciaga（バレンシアガ）', 'バレンシアガ,BALENCIAGA', 9),
        (1, 'Saint Laurent', 'Saint Laurent（サンローラン）', 'サンローラン,YSL', 10),
        (1, 'Loewe', 'Loewe（ロエベ）', 'ロエベ,LOEWE', 11),
        (1, 'Fendi', 'Fendi（フェンディ）', 'フェンディ,FENDI', 12),
        (1, 'Burberry', 'Burberry（バーバリー）', 'バーバリー,BURBERRY', 13),
        (1, 'Valentino', 'Valentino（ヴァレンティノ）', 'ヴァレンティノ,VALENTINO', 14),
        (1, 'Givenchy', 'Givenchy（ジバンシィ）', 'ジバンシィ,GIVENCHY', 15),
        (1, 'Miu Miu', 'Miu Miu（ミュウミュウ）', 'ミュウミュウ,MIU MIU', 16),
        # 時計・ジュエリー
        (2, 'Rolex', 'Rolex（ロレックス）', 'ロレックス,ROLEX,サブマリーナ,デイトナ', 1),
        (2, 'Cartier', 'Cartier（カルティエ）', 'カルティエ,CARTIER', 2),
        (2, 'Omega', 'Omega（オメガ）', 'オメガ,OMEGA,スピードマスター', 3),
        (2, 'Patek Philippe', 'Patek Philippe（パテック・フィリップ）', 'パテック,PATEK', 4),
        (2, 'Audemars Piguet', 'Audemars Piguet（オーデマ・ピゲ）', 'オーデマピゲ,AP,ロイヤルオーク', 5),
        (2, 'Tiffany', 'Tiffany（ティファニー）', 'ティファニー,TIFFANY', 6),
        (2, 'Bvlgari', 'Bvlgari（ブルガリ）', 'ブルガリ,BVLGARI', 7),
        (2, 'Van Cleef', 'Van Cleef（ヴァンクリーフ）', 'ヴァンクリ,VAN CLEEF', 8),
        # スポーツ・ストリート
        (3, 'Nike', 'Nike（ナイキ）', 'ナイキ,NIKE,エアマックス,エアフォース', 1),
        (3, 'Adidas', 'Adidas（アディダス）', 'アディダス,ADIDAS,イージー,YEEZY', 2),
        (3, 'Supreme', 'Supreme（シュプリーム）', 'シュプリーム,SUPREME', 3),
        (3, 'Off-White', 'Off-White（オフホワイト）', 'オフホワイト,OFF-WHITE', 4),
        (3, 'A BATHING APE', 'A BATHING APE（ベイプ）', 'ベイプ,BAPE,エイプ', 5),
        (3, 'Jordan', 'Jordan（ジョーダン）', 'ジョーダン,JORDAN,AJ1', 6),
        (3, 'New Balance', 'New Balance（ニューバランス）', 'ニューバランス,NEW BALANCE,NB', 7),
        # 日本ブランド
        (4, 'COMME des GARCONS', 'COMME des GARCONS（コムデギャルソン）', 'コムデギャルソン,ギャルソン,CDG', 1),
        (4, 'Yohji Yamamoto', 'Yohji Yamamoto（ヨウジヤマモト）', 'ヨウジ,YOHJI', 2),
        (4, 'ISSEY MIYAKE', 'ISSEY MIYAKE（イッセイミヤケ）', 'イッセイミヤケ,ミヤケ', 3),
        (4, 'sacai', 'sacai（サカイ）', 'サカイ,SACAI', 4),
        (4, 'UNDERCOVER', 'UNDERCOVER（アンダーカバー）', 'アンダーカバー,UNDERCOVER', 5),
        # 電子機器
        (5, 'Apple', 'Apple（アップル）', 'アップル,APPLE,iPhone,iPad,MacBook', 1),
        (5, 'Sony', 'Sony（ソニー）', 'ソニー,SONY,プレステ,PlayStation', 2),
        (5, 'Nintendo', 'Nintendo（任天堂）', '任天堂,ニンテンドー,Switch', 3),
        (5, 'Dyson', 'Dyson（ダイソン）', 'ダイソン,DYSON', 4),
        (5, 'Bose', 'Bose（ボーズ）', 'ボーズ,BOSE', 5),
        # その他
        (6, 'その他', 'その他', '', 1),
        (6, 'ノーブランド', 'ノーブランド', '', 2),
    ]
    
    default_suppliers = [
        ('個人', '個人', 1),
        ('代行サービス', '代行サービス', 2),
        ('オークション', 'オークション', 3),
    ]
    
    default_conditions = [
        ('N', 'N：新品', '新品', 1),
        ('S', 'S：新品ではないが傷なし', '新品ではないが傷なし', 2),
        ('A', 'A：未使用に近い', '未使用に近い（小さい傷や汚れ）', 3),
        ('AB', 'AB：傷・汚れあり（小）', '傷・汚れあり（小）', 4),
        ('B', 'B：傷・汚れあり（大）', '傷・汚れあり（大）', 5),
    ]
    
    default_payment_methods = [
        ('現金', '現金', 1),
        ('クレジット', 'クレジット', 2),
        ('PayPay', 'PayPay', 3),
        ('その他', 'その他', 4),
    ]
    
    default_supplier_details = [
        ('業者', '業者', 1),
        ('クライアント', 'クライアント', 2),
        ('個人顧客', '個人顧客', 3),
    ]
    
    if DATABASE_URL:
        cur = conn.cursor()
        
        # ブランドカテゴリ
        cur.execute("SELECT COUNT(*) FROM master_brand_categories")
        if cur.fetchone()[0] == 0:
            for name, order in default_brand_categories:
                cur.execute("INSERT INTO master_brand_categories (name, display_order) VALUES (%s, %s)", (name, order))
        
        # ブランド
        cur.execute("SELECT COUNT(*) FROM master_brands")
        if cur.fetchone()[0] == 0:
            for cat_id, value, display_name, keywords, order in default_brands:
                cur.execute("INSERT INTO master_brands (category_id, value, display_name, keywords, display_order) VALUES (%s, %s, %s, %s, %s)",
                          (cat_id, value, display_name, keywords, order))
        
        # 仕入先
        cur.execute("SELECT COUNT(*) FROM master_suppliers")
        if cur.fetchone()[0] == 0:
            for value, display_name, order in default_suppliers:
                cur.execute("INSERT INTO master_suppliers (value, display_name, display_order) VALUES (%s, %s, %s)", (value, display_name, order))
        
        # 商品状態
        cur.execute("SELECT COUNT(*) FROM master_conditions")
        if cur.fetchone()[0] == 0:
            for value, display_name, desc, order in default_conditions:
                cur.execute("INSERT INTO master_conditions (value, display_name, description, display_order) VALUES (%s, %s, %s, %s)",
                          (value, display_name, desc, order))
        
        # 支払方法
        cur.execute("SELECT COUNT(*) FROM master_payment_methods")
        if cur.fetchone()[0] == 0:
            for value, display_name, order in default_payment_methods:
                cur.execute("INSERT INTO master_payment_methods (value, display_name, display_order) VALUES (%s, %s, %s)", (value, display_name, order))
        
        # 仕入先詳細
        cur.execute("SELECT COUNT(*) FROM master_supplier_details")
        if cur.fetchone()[0] == 0:
            for value, display_name, order in default_supplier_details:
                cur.execute("INSERT INTO master_supplier_details (value, display_name, display_order) VALUES (%s, %s, %s)", (value, display_name, order))
        
        # 書類設定（初期化）
        cur.execute("SELECT COUNT(*) FROM master_document_settings")
        if cur.fetchone()[0] == 0:
            default_doc_settings = [
                ('company_name', '株式会社 開花', 'text', 'company', '会社名', 1),
                ('company_address', '', 'text', 'company', '住所', 2),
                ('company_phone', '', 'text', 'company', '電話番号', 3),
                ('company_fax', '', 'text', 'company', 'FAX番号', 4),
                ('company_email', '', 'text', 'company', 'メールアドレス', 5),
                ('bank_name', '', 'text', 'bank', '銀行名', 1),
                ('bank_branch', '', 'text', 'bank', '支店名', 2),
                ('bank_account_type', '普通', 'text', 'bank', '口座種別', 3),
                ('bank_account_number', '', 'text', 'bank', '口座番号', 4),
                ('bank_account_name', '', 'text', 'bank', '口座名義', 5),
                ('seisan_default_commission_rate', '10', 'number', 'seisan', '精算書デフォルト手数料率（%）', 1),
                ('seisan_default_notes', '', 'textarea', 'seisan', '精算書デフォルト備考', 2),
                ('kaitori_default_tax_rate', '10', 'number', 'kaitori', '買取明細書デフォルト消費税率（%）', 1),
                ('kaitori_default_notes', '', 'textarea', 'kaitori', '買取明細書デフォルト備考', 2),
                ('shikiriosho_default_notes', '', 'textarea', 'shikiriosho', '仕切押し書デフォルト備考', 1),
                ('shikiriosho_default_payment_terms', '30日以内', 'text', 'shikiriosho', '仕切押し書デフォルト支払条件', 2),
                ('invoice_default_tax_rate', '10', 'number', 'invoice', '精算書デフォルト消費税率（%）', 1),
                ('invoice_default_payment_terms', '30日以内', 'text', 'invoice', '精算書デフォルト支払期限', 2),
                ('invoice_default_notes', '', 'textarea', 'invoice', '精算書デフォルト備考', 3),
                ('mitsumori_default_valid_days', '30', 'number', 'mitsumori', '見積依頼書デフォルト有効期限（日）', 1),
                ('mitsumori_default_notes', '', 'textarea', 'mitsumori', '見積依頼書デフォルト備考', 2),
                ('keisan_default_tax_rate', '10', 'number', 'keisan', '計算書デフォルト消費税率（%）', 1),
                ('keisan_default_notes', '', 'textarea', 'keisan', '計算書デフォルト備考', 2),
            ]
            for key, value, stype, cat, display, order in default_doc_settings:
                cur.execute("INSERT INTO master_document_settings (setting_key, setting_value, setting_type, category, display_name, display_order) VALUES (%s, %s, %s, %s, %s, %s)",
                          (key, value, stype, cat, display, order))
    else:
        cur = conn.cursor()
        
        # ブランドカテゴリ
        cur.execute("SELECT COUNT(*) FROM master_brand_categories")
        if cur.fetchone()[0] == 0:
            for name, order in default_brand_categories:
                cur.execute("INSERT INTO master_brand_categories (name, display_order) VALUES (?, ?)", (name, order))
        
        # ブランド
        cur.execute("SELECT COUNT(*) FROM master_brands")
        if cur.fetchone()[0] == 0:
            for cat_id, value, display_name, keywords, order in default_brands:
                cur.execute("INSERT INTO master_brands (category_id, value, display_name, keywords, display_order) VALUES (?, ?, ?, ?, ?)",
                          (cat_id, value, display_name, keywords, order))
        
        # 仕入先
        cur.execute("SELECT COUNT(*) FROM master_suppliers")
        if cur.fetchone()[0] == 0:
            for value, display_name, order in default_suppliers:
                cur.execute("INSERT INTO master_suppliers (value, display_name, display_order) VALUES (?, ?, ?)", (value, display_name, order))
        
        # 商品状態
        cur.execute("SELECT COUNT(*) FROM master_conditions")
        if cur.fetchone()[0] == 0:
            for value, display_name, desc, order in default_conditions:
                cur.execute("INSERT INTO master_conditions (value, display_name, description, display_order) VALUES (?, ?, ?, ?)",
                          (value, display_name, desc, order))
        
        # 支払方法
        cur.execute("SELECT COUNT(*) FROM master_payment_methods")
        if cur.fetchone()[0] == 0:
            for value, display_name, order in default_payment_methods:
                cur.execute("INSERT INTO master_payment_methods (value, display_name, display_order) VALUES (?, ?, ?)", (value, display_name, order))
        
        # 仕入先詳細
        cur.execute("SELECT COUNT(*) FROM master_supplier_details")
        if cur.fetchone()[0] == 0:
            for value, display_name, order in default_supplier_details:
                cur.execute("INSERT INTO master_supplier_details (value, display_name, display_order) VALUES (?, ?, ?)", (value, display_name, order))
        
        # 書類設定（初期化）
        cur.execute("SELECT COUNT(*) FROM master_document_settings")
        if cur.fetchone()[0] == 0:
            default_doc_settings = [
                ('company_name', '株式会社 開花', 'text', 'company', '会社名', 1),
                ('company_address', '', 'text', 'company', '住所', 2),
                ('company_phone', '', 'text', 'company', '電話番号', 3),
                ('company_fax', '', 'text', 'company', 'FAX番号', 4),
                ('company_email', '', 'text', 'company', 'メールアドレス', 5),
                ('bank_name', '', 'text', 'bank', '銀行名', 1),
                ('bank_branch', '', 'text', 'bank', '支店名', 2),
                ('bank_account_type', '普通', 'text', 'bank', '口座種別', 3),
                ('bank_account_number', '', 'text', 'bank', '口座番号', 4),
                ('bank_account_name', '', 'text', 'bank', '口座名義', 5),
                ('seisan_default_commission_rate', '10', 'number', 'seisan', '精算書デフォルト手数料率（%）', 1),
                ('seisan_default_notes', '', 'textarea', 'seisan', '精算書デフォルト備考', 2),
                ('kaitori_default_tax_rate', '10', 'number', 'kaitori', '買取明細書デフォルト消費税率（%）', 1),
                ('kaitori_default_notes', '', 'textarea', 'kaitori', '買取明細書デフォルト備考', 2),
                ('shikiriosho_default_notes', '', 'textarea', 'shikiriosho', '仕切押し書デフォルト備考', 1),
                ('shikiriosho_default_payment_terms', '30日以内', 'text', 'shikiriosho', '仕切押し書デフォルト支払条件', 2),
                ('invoice_default_tax_rate', '10', 'number', 'invoice', '精算書デフォルト消費税率（%）', 1),
                ('invoice_default_payment_terms', '30日以内', 'text', 'invoice', '精算書デフォルト支払期限', 2),
                ('invoice_default_notes', '', 'textarea', 'invoice', '精算書デフォルト備考', 3),
                ('mitsumori_default_valid_days', '30', 'number', 'mitsumori', '見積依頼書デフォルト有効期限（日）', 1),
                ('mitsumori_default_notes', '', 'textarea', 'mitsumori', '見積依頼書デフォルト備考', 2),
                ('keisan_default_tax_rate', '10', 'number', 'keisan', '計算書デフォルト消費税率（%）', 1),
                ('keisan_default_notes', '', 'textarea', 'keisan', '計算書デフォルト備考', 2),
            ]
            for key, value, stype, cat, display, order in default_doc_settings:
                cur.execute("INSERT INTO master_document_settings (setting_key, setting_value, setting_type, category, display_name, display_order) VALUES (?, ?, ?, ?, ?, ?)",
                          (key, value, stype, cat, display, order))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('マスターデータを初期登録しました', 'success')
    return redirect(url_for('admin_master_settings'))


@app.route('/admin/master-settings/<table_name>/add', methods=['POST'])
@login_required
@admin_required
def admin_master_add(table_name):
    """マスターデータの追加（開花管理用 - scope='admin'）"""
    valid_tables = ['brand_categories', 'brands', 'suppliers', 'conditions', 'payment_methods', 'supplier_details']
    if table_name not in valid_tables:
        flash('無効なテーブルです', 'error')
        return redirect(url_for('admin_master_settings'))
    
    scope = 'admin'
    
    try:
        conn = get_db()
        
        if table_name == 'brand_categories':
            name = request.form.get('name')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_brand_categories (name, display_order, scope) VALUES (%s, %s, %s)", (name, display_order, scope))
            else:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_brand_categories (name, display_order, scope) VALUES (?, ?, ?)", (name, display_order, scope))
        
        elif table_name == 'brands':
            category_id_str = request.form.get('category_id')
            category_id = int(category_id_str) if category_id_str else None
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            keywords = request.form.get('keywords')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_brands (category_id, value, display_name, keywords, display_order, scope) VALUES (%s, %s, %s, %s, %s, %s)",
                          (category_id, value, display_name, keywords, display_order, scope))
            else:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_brands (category_id, value, display_name, keywords, display_order, scope) VALUES (?, ?, ?, ?, ?, ?)",
                          (category_id, value, display_name, keywords, display_order, scope))
        
        elif table_name == 'suppliers':
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_suppliers (value, display_name, display_order, scope) VALUES (%s, %s, %s, %s)", (value, display_name, display_order, scope))
            else:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_suppliers (value, display_name, display_order, scope) VALUES (?, ?, ?, ?)", (value, display_name, display_order, scope))
        
        elif table_name == 'conditions':
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            description = request.form.get('description')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_conditions (value, display_name, description, display_order, scope) VALUES (%s, %s, %s, %s, %s)",
                          (value, display_name, description, display_order, scope))
            else:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_conditions (value, display_name, description, display_order, scope) VALUES (?, ?, ?, ?, ?)",
                          (value, display_name, description, display_order, scope))
        
        elif table_name == 'payment_methods':
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_payment_methods (value, display_name, display_order, scope) VALUES (%s, %s, %s, %s)", (value, display_name, display_order, scope))
            else:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_payment_methods (value, display_name, display_order, scope) VALUES (?, ?, ?, ?)", (value, display_name, display_order, scope))
        
        elif table_name == 'supplier_details':
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_supplier_details (value, display_name, display_order, scope) VALUES (%s, %s, %s, %s)", (value, display_name, display_order, scope))
            else:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_supplier_details (value, display_name, display_order, scope) VALUES (?, ?, ?, ?)", (value, display_name, display_order, scope))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('追加しました', 'success')
    except Exception as e:
        print(f"Master add error: {e}")
        import traceback
        traceback.print_exc()
        flash(f'追加エラー: {str(e)}', 'error')
    
    return redirect(url_for('admin_master_settings'))


@app.route('/user/master-settings/<table_name>/add', methods=['POST'])
@login_required
@admin_required
def user_master_add(table_name):
    """マスターデータの追加（ユーザー機能用 - scope='user'）"""
    valid_tables = ['brand_categories', 'brands', 'suppliers', 'conditions', 'payment_methods', 'supplier_details']
    if table_name not in valid_tables:
        flash('無効なテーブルです', 'error')
        return redirect(url_for('user_master_settings'))
    
    scope = 'user'
    
    try:
        conn = get_db()
        
        if table_name == 'brand_categories':
            name = request.form.get('name')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_brand_categories (name, display_order, scope) VALUES (%s, %s, %s)", (name, display_order, scope))
            else:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_brand_categories (name, display_order, scope) VALUES (?, ?, ?)", (name, display_order, scope))
        
        elif table_name == 'brands':
            category_id_str = request.form.get('category_id')
            category_id = int(category_id_str) if category_id_str else None
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            keywords = request.form.get('keywords')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_brands (category_id, value, display_name, keywords, display_order, scope) VALUES (%s, %s, %s, %s, %s, %s)",
                          (category_id, value, display_name, keywords, display_order, scope))
            else:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_brands (category_id, value, display_name, keywords, display_order, scope) VALUES (?, ?, ?, ?, ?, ?)",
                          (category_id, value, display_name, keywords, display_order, scope))
        
        elif table_name == 'suppliers':
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_suppliers (value, display_name, display_order, scope) VALUES (%s, %s, %s, %s)", (value, display_name, display_order, scope))
            else:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_suppliers (value, display_name, display_order, scope) VALUES (?, ?, ?, ?)", (value, display_name, display_order, scope))
        
        elif table_name == 'conditions':
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            description = request.form.get('description')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_conditions (value, display_name, description, display_order, scope) VALUES (%s, %s, %s, %s, %s)",
                          (value, display_name, description, display_order, scope))
            else:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_conditions (value, display_name, description, display_order, scope) VALUES (?, ?, ?, ?, ?)",
                          (value, display_name, description, display_order, scope))
        
        elif table_name == 'payment_methods':
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_payment_methods (value, display_name, display_order, scope) VALUES (%s, %s, %s, %s)", (value, display_name, display_order, scope))
            else:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_payment_methods (value, display_name, display_order, scope) VALUES (?, ?, ?, ?)", (value, display_name, display_order, scope))
        
        elif table_name == 'supplier_details':
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_supplier_details (value, display_name, display_order, scope) VALUES (%s, %s, %s, %s)", (value, display_name, display_order, scope))
            else:
                cur = conn.cursor()
                cur.execute("INSERT INTO master_supplier_details (value, display_name, display_order, scope) VALUES (?, ?, ?, ?)", (value, display_name, display_order, scope))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('追加しました', 'success')
    except Exception as e:
        print(f"User master add error: {e}")
        import traceback
        traceback.print_exc()
        flash(f'追加エラー: {str(e)}', 'error')
    
    return redirect(url_for('user_master_settings'))


@app.route('/admin/master-settings/<table_name>/<int:id>/edit', methods=['POST'])
@login_required
@admin_required
def admin_master_edit(table_name, id):
    """マスターデータの編集"""
    valid_tables = ['brand_categories', 'brands', 'suppliers', 'conditions', 'payment_methods', 'supplier_details']
    if table_name not in valid_tables:
        flash('無効なテーブルです', 'error')
        return redirect(url_for('admin_master_settings'))
    
    try:
        conn = get_db()
        full_table_name = f"master_{table_name}"
        
        # PostgreSQLではブール値として扱う
        is_active_value = True if request.form.get('is_active') else False
        is_active_sqlite = 1 if request.form.get('is_active') else 0
        
        if table_name == 'brand_categories':
            name = request.form.get('name')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET name=%s, display_order=%s, is_active=%s WHERE id=%s",
                          (name, display_order, is_active_value, id))
            else:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET name=?, display_order=?, is_active=? WHERE id=?",
                          (name, display_order, is_active_sqlite, id))
        
        elif table_name == 'brands':
            category_id_str = request.form.get('category_id')
            category_id = int(category_id_str) if category_id_str else None
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            keywords = request.form.get('keywords')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET category_id=%s, value=%s, display_name=%s, keywords=%s, display_order=%s, is_active=%s WHERE id=%s",
                          (category_id, value, display_name, keywords, display_order, is_active_value, id))
            else:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET category_id=?, value=?, display_name=?, keywords=?, display_order=?, is_active=? WHERE id=?",
                          (category_id, value, display_name, keywords, display_order, is_active_sqlite, id))
        
        elif table_name == 'conditions':
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            description = request.form.get('description')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET value=%s, display_name=%s, description=%s, display_order=%s, is_active=%s WHERE id=%s",
                          (value, display_name, description, display_order, is_active_value, id))
            else:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET value=?, display_name=?, description=?, display_order=?, is_active=? WHERE id=?",
                          (value, display_name, description, display_order, is_active_sqlite, id))
        
        else:
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET value=%s, display_name=%s, display_order=%s, is_active=%s WHERE id=%s",
                          (value, display_name, display_order, is_active_value, id))
            else:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET value=?, display_name=?, display_order=?, is_active=? WHERE id=?",
                          (value, display_name, display_order, is_active_sqlite, id))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('更新しました', 'success')
    except Exception as e:
        print(f"Master edit error: {e}")
        import traceback
        traceback.print_exc()
        flash(f'更新エラー: {str(e)}', 'error')
    
    return redirect(url_for('admin_master_settings'))


@app.route('/admin/master-settings/<table_name>/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_master_delete(table_name, id):
    """マスターデータの削除"""
    valid_tables = ['brand_categories', 'brands', 'suppliers', 'conditions', 'payment_methods', 'supplier_details']
    if table_name not in valid_tables:
        flash('無効なテーブルです', 'error')
        return redirect(url_for('admin_master_settings'))
    
    try:
        conn = get_db()
        full_table_name = f"master_{table_name}"
        
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {full_table_name} WHERE id=%s", (id,))
        else:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {full_table_name} WHERE id=?", (id,))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('削除しました', 'success')
    except Exception as e:
        print(f"Master delete error: {e}")
        import traceback
        traceback.print_exc()
        flash(f'削除エラー: {str(e)}', 'error')
    
    return redirect(url_for('admin_master_settings'))


@app.route('/user/master-settings/<table_name>/<int:id>/edit', methods=['POST'])
@login_required
@admin_required
def user_master_edit(table_name, id):
    """ユーザー機能用マスターデータの編集"""
    valid_tables = ['brand_categories', 'brands', 'suppliers', 'conditions', 'payment_methods', 'supplier_details']
    if table_name not in valid_tables:
        flash('無効なテーブルです', 'error')
        return redirect(url_for('user_master_settings'))
    
    try:
        conn = get_db()
        full_table_name = f"master_{table_name}"
        
        is_active_value = True if request.form.get('is_active') else False
        is_active_sqlite = 1 if request.form.get('is_active') else 0
        
        if table_name == 'brand_categories':
            name = request.form.get('name')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET name=%s, display_order=%s, is_active=%s WHERE id=%s",
                          (name, display_order, is_active_value, id))
            else:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET name=?, display_order=?, is_active=? WHERE id=?",
                          (name, display_order, is_active_sqlite, id))
        
        elif table_name == 'brands':
            category_id_str = request.form.get('category_id')
            category_id = int(category_id_str) if category_id_str else None
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            keywords = request.form.get('keywords')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET category_id=%s, value=%s, display_name=%s, keywords=%s, display_order=%s, is_active=%s WHERE id=%s",
                          (category_id, value, display_name, keywords, display_order, is_active_value, id))
            else:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET category_id=?, value=?, display_name=?, keywords=?, display_order=?, is_active=? WHERE id=?",
                          (category_id, value, display_name, keywords, display_order, is_active_sqlite, id))
        
        elif table_name == 'conditions':
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            description = request.form.get('description')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET value=%s, display_name=%s, description=%s, display_order=%s, is_active=%s WHERE id=%s",
                          (value, display_name, description, display_order, is_active_value, id))
            else:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET value=?, display_name=?, description=?, display_order=?, is_active=? WHERE id=?",
                          (value, display_name, description, display_order, is_active_sqlite, id))
        
        else:
            value = request.form.get('value')
            display_name = request.form.get('display_name')
            display_order = int(request.form.get('display_order') or 0)
            if DATABASE_URL:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET value=%s, display_name=%s, display_order=%s, is_active=%s WHERE id=%s",
                          (value, display_name, display_order, is_active_value, id))
            else:
                cur = conn.cursor()
                cur.execute(f"UPDATE {full_table_name} SET value=?, display_name=?, display_order=?, is_active=? WHERE id=?",
                          (value, display_name, display_order, is_active_sqlite, id))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('更新しました', 'success')
    except Exception as e:
        print(f"User master edit error: {e}")
        import traceback
        traceback.print_exc()
        flash(f'更新エラー: {str(e)}', 'error')
    
    return redirect(url_for('user_master_settings'))


@app.route('/user/master-settings/<table_name>/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def user_master_delete(table_name, id):
    """ユーザー機能用マスターデータの削除"""
    valid_tables = ['brand_categories', 'brands', 'suppliers', 'conditions', 'payment_methods', 'supplier_details']
    if table_name not in valid_tables:
        flash('無効なテーブルです', 'error')
        return redirect(url_for('user_master_settings'))
    
    try:
        conn = get_db()
        full_table_name = f"master_{table_name}"
        
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {full_table_name} WHERE id=%s", (id,))
        else:
            cur = conn.cursor()
            cur.execute(f"DELETE FROM {full_table_name} WHERE id=?", (id,))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('削除しました', 'success')
    except Exception as e:
        print(f"User master delete error: {e}")
        import traceback
        traceback.print_exc()
        flash(f'削除エラー: {str(e)}', 'error')
    
    return redirect(url_for('user_master_settings'))


@app.route('/user/master-settings/init', methods=['POST'])
@login_required
@admin_required
def user_master_settings_init():
    """ユーザー機能用マスターデータの初期登録（管理者設定からコピー）"""
    conn = get_db()
    target_scope = 'user'
    source_scope = 'admin'
    
    try:
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            # 1. ブランドカテゴリをコピー（IDマッピング用に保持）
            cur.execute("SELECT * FROM master_brand_categories WHERE scope = %s OR scope IS NULL ORDER BY display_order, id", (source_scope,))
            admin_categories = cur.fetchall()
            category_id_map = {}  # admin_id -> user_id
            
            for cat in admin_categories:
                cur.execute("""
                    INSERT INTO master_brand_categories (name, display_order, is_active, scope)
                    VALUES (%s, %s, %s, %s) RETURNING id
                """, (cat['name'], cat['display_order'], cat['is_active'], target_scope))
                new_id = cur.fetchone()['id']
                category_id_map[cat['id']] = new_id
            
            # 2. ブランド名をコピー（カテゴリIDを変換）
            cur.execute("SELECT * FROM master_brands WHERE scope = %s OR scope IS NULL ORDER BY display_order, id", (source_scope,))
            admin_brands = cur.fetchall()
            
            for brand in admin_brands:
                new_category_id = category_id_map.get(brand['category_id']) if brand['category_id'] else None
                cur.execute("""
                    INSERT INTO master_brands (category_id, value, display_name, keywords, display_order, is_active, scope)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (new_category_id, brand['value'], brand['display_name'], brand['keywords'], brand['display_order'], brand['is_active'], target_scope))
            
            # 3. 仕入先をコピー
            cur.execute("SELECT * FROM master_suppliers WHERE scope = %s OR scope IS NULL ORDER BY display_order, id", (source_scope,))
            admin_suppliers = cur.fetchall()
            
            for item in admin_suppliers:
                cur.execute("""
                    INSERT INTO master_suppliers (value, display_name, display_order, is_active, scope)
                    VALUES (%s, %s, %s, %s, %s)
                """, (item['value'], item['display_name'], item['display_order'], item['is_active'], target_scope))
            
            # 4. 商品状態をコピー
            cur.execute("SELECT * FROM master_conditions WHERE scope = %s OR scope IS NULL ORDER BY display_order, id", (source_scope,))
            admin_conditions = cur.fetchall()
            
            for item in admin_conditions:
                cur.execute("""
                    INSERT INTO master_conditions (value, display_name, description, display_order, is_active, scope)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (item['value'], item['display_name'], item.get('description', ''), item['display_order'], item['is_active'], target_scope))
            
            # 5. 支払方法をコピー
            cur.execute("SELECT * FROM master_payment_methods WHERE scope = %s OR scope IS NULL ORDER BY display_order, id", (source_scope,))
            admin_payment_methods = cur.fetchall()
            
            for item in admin_payment_methods:
                cur.execute("""
                    INSERT INTO master_payment_methods (value, display_name, display_order, is_active, scope)
                    VALUES (%s, %s, %s, %s, %s)
                """, (item['value'], item['display_name'], item['display_order'], item['is_active'], target_scope))
            
            # 6. 仕入先詳細をコピー
            cur.execute("SELECT * FROM master_supplier_details WHERE scope = %s OR scope IS NULL ORDER BY display_order, id", (source_scope,))
            admin_supplier_details = cur.fetchall()
            
            for item in admin_supplier_details:
                cur.execute("""
                    INSERT INTO master_supplier_details (value, display_name, display_order, is_active, scope)
                    VALUES (%s, %s, %s, %s, %s)
                """, (item['value'], item['display_name'], item['display_order'], item['is_active'], target_scope))
        else:
            cur = conn.cursor()
            cur.row_factory = sqlite3.Row
            
            # 1. ブランドカテゴリをコピー
            cur.execute("SELECT * FROM master_brand_categories WHERE scope = ? OR scope IS NULL ORDER BY display_order, id", (source_scope,))
            admin_categories = [dict(row) for row in cur.fetchall()]
            category_id_map = {}
            
            for cat in admin_categories:
                cur.execute("""
                    INSERT INTO master_brand_categories (name, display_order, is_active, scope)
                    VALUES (?, ?, ?, ?)
                """, (cat['name'], cat['display_order'], cat['is_active'], target_scope))
                new_id = cur.lastrowid
                category_id_map[cat['id']] = new_id
            
            # 2. ブランド名をコピー
            cur.execute("SELECT * FROM master_brands WHERE scope = ? OR scope IS NULL ORDER BY display_order, id", (source_scope,))
            admin_brands = [dict(row) for row in cur.fetchall()]
            
            for brand in admin_brands:
                new_category_id = category_id_map.get(brand['category_id']) if brand['category_id'] else None
                cur.execute("""
                    INSERT INTO master_brands (category_id, value, display_name, keywords, display_order, is_active, scope)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (new_category_id, brand['value'], brand['display_name'], brand['keywords'], brand['display_order'], brand['is_active'], target_scope))
            
            # 3. 仕入先をコピー
            cur.execute("SELECT * FROM master_suppliers WHERE scope = ? OR scope IS NULL ORDER BY display_order, id", (source_scope,))
            admin_suppliers = [dict(row) for row in cur.fetchall()]
            
            for item in admin_suppliers:
                cur.execute("""
                    INSERT INTO master_suppliers (value, display_name, display_order, is_active, scope)
                    VALUES (?, ?, ?, ?, ?)
                """, (item['value'], item['display_name'], item['display_order'], item['is_active'], target_scope))
            
            # 4. 商品状態をコピー
            cur.execute("SELECT * FROM master_conditions WHERE scope = ? OR scope IS NULL ORDER BY display_order, id", (source_scope,))
            admin_conditions = [dict(row) for row in cur.fetchall()]
            
            for item in admin_conditions:
                cur.execute("""
                    INSERT INTO master_conditions (value, display_name, description, display_order, is_active, scope)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (item['value'], item['display_name'], item.get('description', ''), item['display_order'], item['is_active'], target_scope))
            
            # 5. 支払方法をコピー
            cur.execute("SELECT * FROM master_payment_methods WHERE scope = ? OR scope IS NULL ORDER BY display_order, id", (source_scope,))
            admin_payment_methods = [dict(row) for row in cur.fetchall()]
            
            for item in admin_payment_methods:
                cur.execute("""
                    INSERT INTO master_payment_methods (value, display_name, display_order, is_active, scope)
                    VALUES (?, ?, ?, ?, ?)
                """, (item['value'], item['display_name'], item['display_order'], item['is_active'], target_scope))
            
            # 6. 仕入先詳細をコピー
            cur.execute("SELECT * FROM master_supplier_details WHERE scope = ? OR scope IS NULL ORDER BY display_order, id", (source_scope,))
            admin_supplier_details = [dict(row) for row in cur.fetchall()]
            
            for item in admin_supplier_details:
                cur.execute("""
                    INSERT INTO master_supplier_details (value, display_name, display_order, is_active, scope)
                    VALUES (?, ?, ?, ?, ?)
                """, (item['value'], item['display_name'], item['display_order'], item['is_active'], target_scope))
        
        conn.commit()
        cur.close()
        conn.close()
        flash('管理者用マスター設定をユーザー機能用にコピーしました', 'success')
    except Exception as e:
        print(f"User master init error: {e}")
        flash(f'初期データ登録エラー: {str(e)}', 'error')
    
    return redirect(url_for('user_master_settings'))


@app.route('/admin/master-settings/document-settings', methods=['POST'])
@login_required
@admin_required
def admin_document_settings_save():
    """書類設定の保存"""
    try:
        conn = get_db()
        
        # 設定キーのリスト
        setting_keys = [
            'company_name', 'company_address', 'company_phone', 'company_fax', 'company_email',
            'bank_name', 'bank_branch', 'bank_account_type', 'bank_account_number', 'bank_account_name',
            # 書類名設定
            'doc_name_seisan', 'doc_name_kaitori', 'doc_name_shikiriosho', 
            'doc_name_invoice', 'doc_name_mitsumori', 'doc_name_keisan', 'doc_name_kaitori_shoudaku',
            # 各書類の詳細設定
            'seisan_default_commission_rate', 'seisan_default_notes',
            'kaitori_default_tax_rate', 'kaitori_default_notes',
            'shikiriosho_default_notes', 'shikiriosho_default_payment_terms',
            'invoice_default_tax_rate', 'invoice_default_payment_terms', 'invoice_default_notes',
            'mitsumori_default_valid_days', 'mitsumori_default_notes',
            'keisan_default_tax_rate', 'keisan_default_notes'
        ]
        
        if DATABASE_URL:
            cur = conn.cursor()
            for key in setting_keys:
                value = request.form.get(key, '')
                # 既存のレコードがあれば更新、なければ挿入
                cur.execute("SELECT id FROM master_document_settings WHERE setting_key = %s", (key,))
                if cur.fetchone():
                    cur.execute("UPDATE master_document_settings SET setting_value = %s, updated_at = CURRENT_TIMESTAMP WHERE setting_key = %s", (value, key))
                else:
                    cur.execute("INSERT INTO master_document_settings (setting_key, setting_value) VALUES (%s, %s)", (key, value))
        else:
            cur = conn.cursor()
            for key in setting_keys:
                value = request.form.get(key, '')
                cur.execute("SELECT id FROM master_document_settings WHERE setting_key = ?", (key,))
                if cur.fetchone():
                    cur.execute("UPDATE master_document_settings SET setting_value = ?, updated_at = CURRENT_TIMESTAMP WHERE setting_key = ?", (value, key))
                else:
                    cur.execute("INSERT INTO master_document_settings (setting_key, setting_value) VALUES (?, ?)", (key, value))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('書類設定を保存しました', 'success')
    except Exception as e:
        print(f"Document settings save error: {e}")
        import traceback
        traceback.print_exc()
        flash(f'設定保存エラー: {str(e)}', 'error')
    
    return redirect(url_for('admin_master_settings'))


@app.route('/api/master-data')
@login_required
def api_master_data():
    """マスターデータをJSON形式で取得（商品登録フォーム用）
    
    クエリパラメータ:
    - scope: 'user' または 'admin'（デフォルト: ユーザーの役割に応じて自動判定）
    """
    conn = get_db()
    
    # スコープの決定: URLパラメータ > ユーザー役割
    scope = request.args.get('scope')
    if not scope:
        # ユーザーの役割に応じてスコープを決定
        if current_user.is_authenticated and current_user.is_admin():
            scope = 'admin'
        else:
            scope = 'user'
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # スコープに応じたデータを取得（該当スコープまたはNULL）
        cur.execute("SELECT * FROM master_brand_categories WHERE is_active = TRUE AND (scope = %s OR scope IS NULL) ORDER BY display_order, id", (scope,))
        brand_categories = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_brands WHERE is_active = TRUE AND (scope = %s OR scope IS NULL) ORDER BY display_order, id", (scope,))
        brands = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_suppliers WHERE is_active = TRUE AND (scope = %s OR scope IS NULL) ORDER BY display_order, id", (scope,))
        suppliers = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_conditions WHERE is_active = TRUE AND (scope = %s OR scope IS NULL) ORDER BY display_order, id", (scope,))
        conditions = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_payment_methods WHERE is_active = TRUE AND (scope = %s OR scope IS NULL) ORDER BY display_order, id", (scope,))
        payment_methods = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_supplier_details WHERE is_active = TRUE AND (scope = %s OR scope IS NULL) ORDER BY display_order, id", (scope,))
        supplier_details = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.row_factory = sqlite3.Row
        
        cur.execute("SELECT * FROM master_brand_categories WHERE is_active = 1 AND (scope = ? OR scope IS NULL) ORDER BY display_order, id", (scope,))
        brand_categories = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_brands WHERE is_active = 1 AND (scope = ? OR scope IS NULL) ORDER BY display_order, id", (scope,))
        brands = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_suppliers WHERE is_active = 1 AND (scope = ? OR scope IS NULL) ORDER BY display_order, id", (scope,))
        suppliers = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_conditions WHERE is_active = 1 AND (scope = ? OR scope IS NULL) ORDER BY display_order, id", (scope,))
        conditions = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_payment_methods WHERE is_active = 1 AND (scope = ? OR scope IS NULL) ORDER BY display_order, id", (scope,))
        payment_methods = [dict(row) for row in cur.fetchall()]
        
        cur.execute("SELECT * FROM master_supplier_details WHERE is_active = 1 AND (scope = ? OR scope IS NULL) ORDER BY display_order, id", (scope,))
        supplier_details = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return jsonify({
        'brand_categories': brand_categories,
        'brands': brands,
        'suppliers': suppliers,
        'conditions': conditions,
        'payment_methods': payment_methods,
        'supplier_details': supplier_details,
        'scope': scope
    })


# ===================
# バックアップ・リストア
# ===================

@app.route('/admin/backup')
@login_required
@permission_required('backup')
def admin_backup():
    return render_template('admin/backup.html')

@app.route('/admin/backup/export')
@login_required
@permission_required('backup')
def export_backup():
    """全データをJSON形式でエクスポート"""
    conn = get_db()
    backup_data = {
        'exported_at': datetime.now().isoformat(),
        'version': '3.2',
        'users': [],
        'merchandise': [],
        'customers': [],
        'sale_requests': [],
        'shikiriosho': [],
        'shikiriosho_items': [],
        'invoices': [],
        'invoice_items': [],
        'user_mitsumori': [],
        'user_mitsumori_items': [],
        'user_keisan': [],
        'user_keisan_items': [],
        'user_kaitori_shoudaku': [],
        'user_kaitori_shoudaku_items': [],
        'service_documents': [],
        'announcements': [],
        'master_brand_categories': [],
        'master_brands': [],
        'master_suppliers': [],
        'master_conditions': [],
        'master_payment_methods': [],
        'master_supplier_details': [],
        'master_document_settings': [],
        'proxy_service_settings': [],
        'line_settings': [],
        'line_scheduled_messages': [],
        # v3.1で追加：問い合わせ、管理者用買取承諾書、処分申請
        'inquiries': [],
        'inquiry_replies': [],
        'admin_kaitori_shoudaku': [],
        'admin_kaitori_shoudaku_items': [],
        'item_disposal_requests': [],
        # v3.2で追加：代行サービス関連
        'proxy_service_users': [],
        'proxy_service_bids': [],
        'sales_agency_requests': [],
        'sales_agency_request_items': []
    }
    
    # テーブル一覧（存在しない場合はスキップ）
    tables_to_backup = [
        'users', 'merchandise', 'customers', 'sale_requests',
        'shikiriosho', 'shikiriosho_items', 'invoices', 'invoice_items',
        'user_mitsumori', 'user_mitsumori_items', 'user_keisan', 'user_keisan_items',
        'user_kaitori_shoudaku', 'user_kaitori_shoudaku_items', 'service_documents',
        'announcements', 'master_brand_categories', 'master_brands',
        'master_suppliers', 'master_conditions', 'master_payment_methods',
        'master_supplier_details', 'master_document_settings',
        'proxy_service_settings', 'line_settings', 'line_scheduled_messages',
        # v3.1で追加
        'inquiries', 'inquiry_replies',
        'admin_kaitori_shoudaku', 'admin_kaitori_shoudaku_items',
        'item_disposal_requests',
        # v3.2で追加（代行サービス関連）
        'proxy_service_users', 'proxy_service_bids',
        'sales_agency_requests', 'sales_agency_request_items'
    ]
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        for table in tables_to_backup:
            try:
                cur.execute(f"SELECT * FROM {table} ORDER BY id")
                backup_data[table] = [dict(row) for row in cur.fetchall()]
            except Exception as e:
                print(f"Table {table} backup skipped: {e}")
                backup_data[table] = []
    else:
        cur = conn.cursor()
        for table in tables_to_backup:
            try:
                cur.execute(f"SELECT * FROM {table} ORDER BY id")
                backup_data[table] = [dict(row) for row in cur.fetchall()]
            except Exception as e:
                print(f"Table {table} backup skipped: {e}")
                backup_data[table] = []
    
    cur.close()
    conn.close()
    
    # 日付型をJSON用に変換
    def convert_dates(obj):
        if isinstance(obj, dict):
            return {k: convert_dates(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [convert_dates(item) for item in obj]
        elif hasattr(obj, 'isoformat'):
            return obj.isoformat()
        return obj
    
    backup_data = convert_dates(backup_data)
    
    # JSONファイルとしてダウンロード
    output = io.BytesIO()
    output.write(json.dumps(backup_data, ensure_ascii=False, indent=2).encode('utf-8'))
    output.seek(0)
    
    return send_file(
        output,
        mimetype='application/json',
        as_attachment=True,
        download_name=f'merchandise_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    )

@app.route('/admin/backup/export_with_images')
@login_required
@permission_required('backup')
def export_backup_with_images():
    """全データと画像をZIP形式でエクスポート"""
    conn = get_db()
    backup_data = {
        'exported_at': datetime.now().isoformat(),
        'version': '3.2',
        'includes_images': True,
        'users': [],
        'merchandise': [],
        'customers': [],
        'sale_requests': [],
        'shikiriosho': [],
        'shikiriosho_items': [],
        'invoices': [],
        'invoice_items': [],
        'user_mitsumori': [],
        'user_mitsumori_items': [],
        'user_keisan': [],
        'user_keisan_items': [],
        'user_kaitori_shoudaku': [],
        'user_kaitori_shoudaku_items': [],
        'service_documents': [],
        'announcements': [],
        'master_brand_categories': [],
        'master_brands': [],
        'master_suppliers': [],
        'master_conditions': [],
        'master_payment_methods': [],
        'master_supplier_details': [],
        'master_document_settings': [],
        'proxy_service_settings': [],
        'line_settings': [],
        'line_scheduled_messages': [],
        # v3.1で追加：問い合わせ、管理者用買取承諾書、処分申請
        'inquiries': [],
        'inquiry_replies': [],
        'admin_kaitori_shoudaku': [],
        'admin_kaitori_shoudaku_items': [],
        'item_disposal_requests': [],
        # v3.2で追加：代行サービス関連
        'proxy_service_users': [],
        'proxy_service_bids': [],
        'sales_agency_requests': [],
        'sales_agency_request_items': []
    }
    
    # テーブル一覧（存在しない場合はスキップ）
    tables_to_backup = [
        'users', 'merchandise', 'customers', 'sale_requests',
        'shikiriosho', 'shikiriosho_items', 'invoices', 'invoice_items',
        'user_mitsumori', 'user_mitsumori_items', 'user_keisan', 'user_keisan_items',
        'user_kaitori_shoudaku', 'user_kaitori_shoudaku_items', 'service_documents',
        'announcements', 'master_brand_categories', 'master_brands',
        'master_suppliers', 'master_conditions', 'master_payment_methods',
        'master_supplier_details', 'master_document_settings',
        'proxy_service_settings', 'line_settings', 'line_scheduled_messages',
        # v3.1で追加
        'inquiries', 'inquiry_replies',
        'admin_kaitori_shoudaku', 'admin_kaitori_shoudaku_items',
        'item_disposal_requests',
        # v3.2で追加（代行サービス関連）
        'proxy_service_users', 'proxy_service_bids',
        'sales_agency_requests', 'sales_agency_request_items'
    ]
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        for table in tables_to_backup:
            try:
                cur.execute(f"SELECT * FROM {table} ORDER BY id")
                backup_data[table] = [dict(row) for row in cur.fetchall()]
            except Exception as e:
                print(f"Table {table} backup skipped: {e}")
                backup_data[table] = []
    else:
        cur = conn.cursor()
        for table in tables_to_backup:
            try:
                cur.execute(f"SELECT * FROM {table} ORDER BY id")
                backup_data[table] = [dict(row) for row in cur.fetchall()]
            except Exception as e:
                print(f"Table {table} backup skipped: {e}")
                backup_data[table] = []
    
    cur.close()
    conn.close()
    
    # 日付型をJSON用に変換
    def convert_dates(obj):
        if isinstance(obj, dict):
            return {k: convert_dates(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [convert_dates(item) for item in obj]
        elif hasattr(obj, 'isoformat'):
            return obj.isoformat()
        return obj
    
    backup_data = convert_dates(backup_data)
    
    # ZIPファイルを作成
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # JSONデータを追加
        json_data = json.dumps(backup_data, ensure_ascii=False, indent=2)
        zip_file.writestr('backup_data.json', json_data.encode('utf-8'))
        
        # 画像ファイルを追加するヘルパー関数
        def add_file_to_zip(file_path, added_files):
            if not file_path:
                return
            file_path = file_path.replace('\\', '/')
            if file_path.startswith('uploads/'):
                filename = file_path[8:]
            else:
                filename = os.path.basename(file_path)
            
            full_path = os.path.join(uploads_path, filename)
            if os.path.exists(full_path) and filename not in added_files:
                zip_file.write(full_path, f'images/{filename}')
                added_files.add(filename)
        
        # 画像ファイルを追加
        uploads_path = os.path.join(app.config['UPLOAD_FOLDER'])
        added_files = set()  # 重複防止用
        
        if os.path.exists(uploads_path):
            # 商品の画像
            for item in backup_data['merchandise']:
                # メイン写真
                add_file_to_zip(item.get('photo_path'), added_files)
                
                # 追加写真（2枚目以降）
                additional_photos = item.get('additional_photos')
                if additional_photos:
                    try:
                        if isinstance(additional_photos, str):
                            additional_list = json.loads(additional_photos)
                        else:
                            additional_list = additional_photos
                        for add_photo in additional_list:
                            add_file_to_zip(add_photo, added_files)
                    except (json.JSONDecodeError, TypeError):
                        pass
                
                # 身分証
                add_file_to_zip(item.get('id_document_path'), added_files)
                
                # 同意書
                add_file_to_zip(item.get('consent_form_path'), added_files)
            
            # 売却申請のQRコード画像
            for req in backup_data.get('sale_requests', []):
                add_file_to_zip(req.get('qr_image_path'), added_files)
    
    zip_buffer.seek(0)
    
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'merchandise_full_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'
    )

@app.route('/admin/backup/export_user')
@login_required
def export_user_backup():
    """自分のデータのみをJSON形式でエクスポート"""
    conn = get_db()
    backup_data = {
        'exported_at': datetime.now().isoformat(),
        'version': '3.0',
        'user_id': current_user.id,
        'username': current_user.username,
        'merchandise': [],
        'customers': [],
        'sale_requests': [],
        'user_mitsumori': [],
        'user_mitsumori_items': [],
        'user_keisan': [],
        'user_keisan_items': [],
        'user_kaitori_shoudaku': [],
        'user_kaitori_shoudaku_items': [],
        'invoices': [],
        'invoice_items': [],
        'service_documents': []
    }
    
    # ユーザーIDでフィルタするテーブル
    user_tables = {
        'merchandise': 'user_id',
        'customers': 'user_id',
        'sale_requests': 'user_id',
        'user_mitsumori': 'user_id',
        'user_keisan': 'user_id',
        'user_kaitori_shoudaku': 'user_id',
        'invoices': 'user_id',
        'service_documents': 'user_id'
    }
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        for table, user_col in user_tables.items():
            try:
                cur.execute(f"SELECT * FROM {table} WHERE {user_col} = %s ORDER BY id", (current_user.id,))
                backup_data[table] = [dict(row) for row in cur.fetchall()]
            except Exception as e:
                print(f"Table {table} backup skipped: {e}")
                backup_data[table] = []
        
        # 明細テーブル（親のIDでフィルタ）
        mitsumori_ids = [m['id'] for m in backup_data['user_mitsumori']]
        if mitsumori_ids:
            cur.execute(f"SELECT * FROM user_mitsumori_items WHERE mitsumori_id IN ({','.join(['%s']*len(mitsumori_ids))}) ORDER BY id", tuple(mitsumori_ids))
            backup_data['user_mitsumori_items'] = [dict(row) for row in cur.fetchall()]
        
        keisan_ids = [k['id'] for k in backup_data['user_keisan']]
        if keisan_ids:
            cur.execute(f"SELECT * FROM user_keisan_items WHERE keisan_id IN ({','.join(['%s']*len(keisan_ids))}) ORDER BY id", tuple(keisan_ids))
            backup_data['user_keisan_items'] = [dict(row) for row in cur.fetchall()]
        
        kaitori_ids = [k['id'] for k in backup_data['user_kaitori_shoudaku']]
        if kaitori_ids:
            cur.execute(f"SELECT * FROM user_kaitori_shoudaku_items WHERE kaitori_shoudaku_id IN ({','.join(['%s']*len(kaitori_ids))}) ORDER BY id", tuple(kaitori_ids))
            backup_data['user_kaitori_shoudaku_items'] = [dict(row) for row in cur.fetchall()]
        
        invoice_ids = [i['id'] for i in backup_data['invoices']]
        if invoice_ids:
            cur.execute(f"SELECT * FROM invoice_items WHERE invoice_id IN ({','.join(['%s']*len(invoice_ids))}) ORDER BY id", tuple(invoice_ids))
            backup_data['invoice_items'] = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        for table, user_col in user_tables.items():
            try:
                cur.execute(f"SELECT * FROM {table} WHERE {user_col} = ? ORDER BY id", (current_user.id,))
                backup_data[table] = [dict(row) for row in cur.fetchall()]
            except Exception as e:
                print(f"Table {table} backup skipped: {e}")
                backup_data[table] = []
        
        # 明細テーブル（親のIDでフィルタ）
        mitsumori_ids = [m['id'] for m in backup_data['user_mitsumori']]
        if mitsumori_ids:
            cur.execute(f"SELECT * FROM user_mitsumori_items WHERE mitsumori_id IN ({','.join(['?']*len(mitsumori_ids))}) ORDER BY id", tuple(mitsumori_ids))
            backup_data['user_mitsumori_items'] = [dict(row) for row in cur.fetchall()]
        
        keisan_ids = [k['id'] for k in backup_data['user_keisan']]
        if keisan_ids:
            cur.execute(f"SELECT * FROM user_keisan_items WHERE keisan_id IN ({','.join(['?']*len(keisan_ids))}) ORDER BY id", tuple(keisan_ids))
            backup_data['user_keisan_items'] = [dict(row) for row in cur.fetchall()]
        
        kaitori_ids = [k['id'] for k in backup_data['user_kaitori_shoudaku']]
        if kaitori_ids:
            cur.execute(f"SELECT * FROM user_kaitori_shoudaku_items WHERE kaitori_shoudaku_id IN ({','.join(['?']*len(kaitori_ids))}) ORDER BY id", tuple(kaitori_ids))
            backup_data['user_kaitori_shoudaku_items'] = [dict(row) for row in cur.fetchall()]
        
        invoice_ids = [i['id'] for i in backup_data['invoices']]
        if invoice_ids:
            cur.execute(f"SELECT * FROM invoice_items WHERE invoice_id IN ({','.join(['?']*len(invoice_ids))}) ORDER BY id", tuple(invoice_ids))
            backup_data['invoice_items'] = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    def convert_dates(obj):
        if isinstance(obj, dict):
            return {k: convert_dates(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [convert_dates(item) for item in obj]
        elif hasattr(obj, 'isoformat'):
            return obj.isoformat()
        return obj
    
    backup_data = convert_dates(backup_data)
    
    output = io.BytesIO()
    output.write(json.dumps(backup_data, ensure_ascii=False, indent=2).encode('utf-8'))
    output.seek(0)
    
    return send_file(
        output,
        mimetype='application/json',
        as_attachment=True,
        download_name=f'my_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    )

@app.route('/backup/export_with_images')
@login_required
def export_user_backup_with_images():
    """自分のデータと画像をZIP形式でエクスポート"""
    conn = get_db()
    backup_data = {
        'exported_at': datetime.now().isoformat(),
        'version': '3.0',
        'includes_images': True,
        'user_id': current_user.id,
        'username': current_user.username,
        'merchandise': [],
        'customers': [],
        'sale_requests': [],
        'user_mitsumori': [],
        'user_mitsumori_items': [],
        'user_keisan': [],
        'user_keisan_items': [],
        'user_kaitori_shoudaku': [],
        'user_kaitori_shoudaku_items': [],
        'invoices': [],
        'invoice_items': [],
        'service_documents': []
    }
    
    # ユーザーIDでフィルタするテーブル
    user_tables = {
        'merchandise': 'user_id',
        'customers': 'user_id',
        'sale_requests': 'user_id',
        'user_mitsumori': 'user_id',
        'user_keisan': 'user_id',
        'user_kaitori_shoudaku': 'user_id',
        'invoices': 'user_id',
        'service_documents': 'user_id'
    }
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        for table, user_col in user_tables.items():
            try:
                cur.execute(f"SELECT * FROM {table} WHERE {user_col} = %s ORDER BY id", (current_user.id,))
                backup_data[table] = [dict(row) for row in cur.fetchall()]
            except Exception as e:
                print(f"Table {table} backup skipped: {e}")
                backup_data[table] = []
        
        # 明細テーブル
        mitsumori_ids = [m['id'] for m in backup_data['user_mitsumori']]
        if mitsumori_ids:
            cur.execute(f"SELECT * FROM user_mitsumori_items WHERE mitsumori_id IN ({','.join(['%s']*len(mitsumori_ids))}) ORDER BY id", tuple(mitsumori_ids))
            backup_data['user_mitsumori_items'] = [dict(row) for row in cur.fetchall()]
        
        keisan_ids = [k['id'] for k in backup_data['user_keisan']]
        if keisan_ids:
            cur.execute(f"SELECT * FROM user_keisan_items WHERE keisan_id IN ({','.join(['%s']*len(keisan_ids))}) ORDER BY id", tuple(keisan_ids))
            backup_data['user_keisan_items'] = [dict(row) for row in cur.fetchall()]
        
        kaitori_ids = [k['id'] for k in backup_data['user_kaitori_shoudaku']]
        if kaitori_ids:
            cur.execute(f"SELECT * FROM user_kaitori_shoudaku_items WHERE kaitori_shoudaku_id IN ({','.join(['%s']*len(kaitori_ids))}) ORDER BY id", tuple(kaitori_ids))
            backup_data['user_kaitori_shoudaku_items'] = [dict(row) for row in cur.fetchall()]
        
        invoice_ids = [i['id'] for i in backup_data['invoices']]
        if invoice_ids:
            cur.execute(f"SELECT * FROM invoice_items WHERE invoice_id IN ({','.join(['%s']*len(invoice_ids))}) ORDER BY id", tuple(invoice_ids))
            backup_data['invoice_items'] = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        for table, user_col in user_tables.items():
            try:
                cur.execute(f"SELECT * FROM {table} WHERE {user_col} = ? ORDER BY id", (current_user.id,))
                backup_data[table] = [dict(row) for row in cur.fetchall()]
            except Exception as e:
                print(f"Table {table} backup skipped: {e}")
                backup_data[table] = []
        
        # 明細テーブル
        mitsumori_ids = [m['id'] for m in backup_data['user_mitsumori']]
        if mitsumori_ids:
            cur.execute(f"SELECT * FROM user_mitsumori_items WHERE mitsumori_id IN ({','.join(['?']*len(mitsumori_ids))}) ORDER BY id", tuple(mitsumori_ids))
            backup_data['user_mitsumori_items'] = [dict(row) for row in cur.fetchall()]
        
        keisan_ids = [k['id'] for k in backup_data['user_keisan']]
        if keisan_ids:
            cur.execute(f"SELECT * FROM user_keisan_items WHERE keisan_id IN ({','.join(['?']*len(keisan_ids))}) ORDER BY id", tuple(keisan_ids))
            backup_data['user_keisan_items'] = [dict(row) for row in cur.fetchall()]
        
        kaitori_ids = [k['id'] for k in backup_data['user_kaitori_shoudaku']]
        if kaitori_ids:
            cur.execute(f"SELECT * FROM user_kaitori_shoudaku_items WHERE kaitori_shoudaku_id IN ({','.join(['?']*len(kaitori_ids))}) ORDER BY id", tuple(kaitori_ids))
            backup_data['user_kaitori_shoudaku_items'] = [dict(row) for row in cur.fetchall()]
        
        invoice_ids = [i['id'] for i in backup_data['invoices']]
        if invoice_ids:
            cur.execute(f"SELECT * FROM invoice_items WHERE invoice_id IN ({','.join(['?']*len(invoice_ids))}) ORDER BY id", tuple(invoice_ids))
            backup_data['invoice_items'] = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    def convert_dates(obj):
        if isinstance(obj, dict):
            return {k: convert_dates(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [convert_dates(item) for item in obj]
        elif hasattr(obj, 'isoformat'):
            return obj.isoformat()
        return obj
    
    backup_data = convert_dates(backup_data)
    
    # ZIPファイルを作成
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # JSONデータを追加
        json_data = json.dumps(backup_data, ensure_ascii=False, indent=2)
        zip_file.writestr('backup_data.json', json_data.encode('utf-8'))
        
        # 画像ファイルを追加するヘルパー関数
        def add_file_to_zip(file_path, added_files):
            if not file_path:
                return
            file_path = file_path.replace('\\', '/')
            if file_path.startswith('uploads/'):
                filename = file_path[8:]
            else:
                filename = os.path.basename(file_path)
            
            full_path = os.path.join(uploads_path, filename)
            if os.path.exists(full_path) and filename not in added_files:
                zip_file.write(full_path, f'images/{filename}')
                added_files.add(filename)
        
        # 画像ファイルを追加
        uploads_path = os.path.join(app.config['UPLOAD_FOLDER'])
        added_files = set()  # 重複防止用
        
        if os.path.exists(uploads_path):
            # 商品の画像
            for item in backup_data['merchandise']:
                # メイン写真
                add_file_to_zip(item.get('photo_path'), added_files)
                
                # 追加写真（2枚目以降）
                additional_photos = item.get('additional_photos')
                if additional_photos:
                    try:
                        if isinstance(additional_photos, str):
                            additional_list = json.loads(additional_photos)
                        else:
                            additional_list = additional_photos
                        for add_photo in additional_list:
                            add_file_to_zip(add_photo, added_files)
                    except (json.JSONDecodeError, TypeError):
                        pass
                
                # 身分証
                add_file_to_zip(item.get('id_document_path'), added_files)
                
                # 同意書
                add_file_to_zip(item.get('consent_form_path'), added_files)
            
            # 売却申請のQRコード画像
            for req in backup_data.get('sale_requests', []):
                add_file_to_zip(req.get('qr_image_path'), added_files)
    
    zip_buffer.seek(0)
    
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'my_full_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'
    )

@app.route('/admin/backup/import', methods=['POST'])
@login_required
@permission_required('backup')
def import_backup():
    """JSON/ZIPファイルからデータをインポート（リストア）"""
    if 'backup_file' not in request.files:
        flash('ファイルが選択されていません', 'error')
        return redirect(url_for('admin_backup'))
    
    file = request.files['backup_file']
    if file.filename == '':
        flash('ファイルが選択されていません', 'error')
        return redirect(url_for('admin_backup'))
    
    backup_data = None
    extracted_images = {}
    
    # ZIPファイルの場合
    if file.filename.endswith('.zip'):
        try:
            with zipfile.ZipFile(file, 'r') as zip_file:
                # JSONデータを読み込み
                if 'backup_data.json' in zip_file.namelist():
                    with zip_file.open('backup_data.json') as json_file:
                        backup_data = json.load(json_file)
                
                # 画像ファイルを抽出
                for name in zip_file.namelist():
                    if name.startswith('images/') and not name.endswith('/'):
                        filename = os.path.basename(name)
                        image_data = zip_file.read(name)
                        # uploadsフォルダに保存
                        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                        with open(save_path, 'wb') as f:
                            f.write(image_data)
                        extracted_images[filename] = f'uploads/{filename}'
        except zipfile.BadZipFile:
            flash('無効なZIPファイルです', 'error')
            return redirect(url_for('admin_backup'))
    # JSONファイルの場合
    elif file.filename.endswith('.json'):
        try:
            backup_data = json.load(file)
        except json.JSONDecodeError:
            flash('無効なJSONファイルです', 'error')
            return redirect(url_for('admin_backup'))
    else:
        flash('JSONまたはZIPファイルを選択してください', 'error')
        return redirect(url_for('admin_backup'))
    
    if not backup_data:
        flash('バックアップデータが見つかりません', 'error')
        return redirect(url_for('admin_backup'))
    
    import_mode = request.form.get('import_mode', 'merge')
    
    conn = get_db()
    # PostgreSQLの場合はautocommitモードを有効化（各INSERT文を即座にコミット）
    if DATABASE_URL:
        conn.autocommit = True
    cur = conn.cursor()
    
    try:
        imported_counts = {'users': 0, 'merchandise': 0, 'customers': 0}
        
        # 全削除モードの場合
        if import_mode == 'replace':
            if DATABASE_URL:
                cur.execute("DELETE FROM merchandise")
                cur.execute("DELETE FROM customers")
                cur.execute("DELETE FROM users WHERE id != %s", (current_user.id,))
            else:
                cur.execute("DELETE FROM merchandise")
                cur.execute("DELETE FROM customers")
                cur.execute("DELETE FROM users WHERE id != ?", (current_user.id,))
        
        # ユーザーをインポート（管理者バックアップの場合）
        # 現在ログイン中のユーザーのusernameを取得
        current_username = current_user.username
        
        if 'users' in backup_data:
            for user in backup_data['users']:
                try:
                    # 現在ログイン中のユーザーはスキップ（セッション維持のため）
                    if user.get('username') == current_username:
                        print(f"DEBUG: Skipping current user: {current_username}")
                        continue
                    
                    if DATABASE_URL:
                        cur.execute('''
                            INSERT INTO users (username, email, password_hash, role, display_name, created_at, proxy_service_budget, admin_permissions, subscription_status)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (username) DO UPDATE SET
                                email = EXCLUDED.email,
                                display_name = EXCLUDED.display_name,
                                proxy_service_budget = EXCLUDED.proxy_service_budget,
                                admin_permissions = EXCLUDED.admin_permissions,
                                subscription_status = EXCLUDED.subscription_status
                        ''', (
                            user.get('username'),
                            user.get('email'),
                            user.get('password_hash'),
                            user.get('role', 'user'),
                            user.get('display_name'),
                            user.get('created_at'),
                            user.get('proxy_service_budget', 0),
                            user.get('admin_permissions'),
                            user.get('subscription_status', 'inactive')
                        ))
                    else:
                        # SQLiteの場合: まず存在確認してからINSERTまたはUPDATE
                        cur.execute("SELECT id FROM users WHERE username = ?", (user.get('username'),))
                        existing = cur.fetchone()
                        if existing:
                            # 既存ユーザーは更新（IDを保持）
                            cur.execute('''
                                UPDATE users SET email = ?, display_name = ?, proxy_service_budget = ?, admin_permissions = ?, subscription_status = ?
                                WHERE username = ?
                            ''', (
                                user.get('email'),
                                user.get('display_name'),
                                user.get('proxy_service_budget', 0),
                                user.get('admin_permissions'),
                                user.get('subscription_status', 'inactive'),
                                user.get('username')
                            ))
                        else:
                            # 新規ユーザーは挿入
                            cur.execute('''
                                INSERT INTO users (username, email, password_hash, role, display_name, created_at, proxy_service_budget, admin_permissions, subscription_status)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ''', (
                                user.get('username'),
                                user.get('email'),
                                user.get('password_hash'),
                                user.get('role', 'user'),
                                user.get('display_name'),
                                user.get('created_at'),
                                user.get('proxy_service_budget', 0),
                                user.get('admin_permissions'),
                                user.get('subscription_status', 'inactive')
                            ))
                    imported_counts['users'] += 1
                except Exception as e:
                    if not DATABASE_URL: conn.rollback()  # SQLiteの場合のみrollback
                    print(f"User import error: {e}")
        
        # ユーザーIDマッピングを取得（username → 新user_id）
        user_id_map = {}
        if DATABASE_URL:
            cur.execute("SELECT id, username FROM users")
            for row in cur.fetchall():
                user_id_map[row[1] if isinstance(row, tuple) else row['username']] = row[0] if isinstance(row, tuple) else row['id']
        else:
            cur.execute("SELECT id, username FROM users")
            for row in cur.fetchall():
                user_id_map[row['username']] = row['id']
        
        # バックアップの旧user_id → usernameマッピングを作成
        old_user_id_to_username = {}
        if 'users' in backup_data:
            for u in backup_data['users']:
                old_id = u.get('id')
                username = u.get('username')
                if old_id is not None and username:
                    old_user_id_to_username[old_id] = username
        
        def resolve_user_id(old_user_id):
            """旧user_idを新しい環境のuser_idに変換"""
            if old_user_id is None:
                return current_user.id
            # 旧user_id → username → 新user_id の変換
            if old_user_id in old_user_id_to_username:
                username = old_user_id_to_username[old_user_id]
                if username in user_id_map:
                    return user_id_map[username]
            # マッピングが見つからない場合は現在のユーザーIDを使用
            return current_user.id
        
        # 商品をインポート
        merchandise_list = backup_data.get('merchandise', [])
        print(f"DEBUG: Total merchandise to import: {len(merchandise_list)}", flush=True)
        print(f"DEBUG: user_id_map = {user_id_map}", flush=True)
        print(f"DEBUG: old_user_id_to_username = {old_user_id_to_username}", flush=True)
        for item in merchandise_list:
            try:
                # user_idを解決（旧環境のIDを新環境のIDにマッピング）
                old_user_id = item.get('user_id')
                if 'username' in backup_data and 'users' not in backup_data:
                    # ユーザー個別バックアップの場合、現在のユーザーIDを使用
                    user_id = current_user.id
                else:
                    user_id = resolve_user_id(old_user_id)
                
                print(f"DEBUG: Importing item: {item.get('product_name')} (old_user_id={old_user_id}, new_user_id={user_id})", flush=True)
                
                if DATABASE_URL:
                    cur.execute('''
                        INSERT INTO merchandise (user_id, purchase_date, photo_path, product_name, store_name,
                            purchase_price, payment_method, listing_price, expected_shipping, expected_commission,
                            is_listed, listing_date, sale_date, sale_price, shipping_cost, sales_destination,
                            commission, is_shipped, created_at, brand_name, item_condition, additional_photos,
                            sale_type, model_number, supplier_detail, id_document_path, consent_form_path,
                            updated_by, updated_at, notes, show_in_proxy_service, kaika_product_code)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (
                        user_id,
                        item.get('purchase_date'),
                        item.get('photo_path'),
                        item.get('product_name'),
                        item.get('store_name'),
                        item.get('purchase_price', 0),
                        item.get('payment_method'),
                        item.get('listing_price', 0),
                        item.get('expected_shipping', 0),
                        item.get('expected_commission', 0),
                        bool(item.get('is_listed', False)),  # PostgreSQLのboolean型に変換
                        item.get('listing_date'),
                        item.get('sale_date'),
                        item.get('sale_price', 0),
                        item.get('shipping_cost', 0),
                        item.get('sales_destination'),
                        item.get('commission', 0),
                        bool(item.get('is_shipped', False)),  # PostgreSQLのboolean型に変換
                        item.get('created_at'),
                        item.get('brand_name'),
                        item.get('item_condition'),
                        item.get('additional_photos'),
                        item.get('sale_type', 'normal'),
                        item.get('model_number'),
                        item.get('supplier_detail'),
                        item.get('id_document_path'),
                        item.get('consent_form_path'),
                        resolve_user_id(item.get('updated_by')) if item.get('updated_by') else None,  # 外部キー制約対応
                        item.get('updated_at'),
                        item.get('notes'),
                        bool(item.get('show_in_proxy_service', False)),  # PostgreSQLのboolean型に変換
                        item.get('kaika_product_code')
                    ))
                else:
                    cur.execute('''
                        INSERT INTO merchandise (user_id, purchase_date, photo_path, product_name, store_name,
                            purchase_price, payment_method, listing_price, expected_shipping, expected_commission,
                            is_listed, listing_date, sale_date, sale_price, shipping_cost, sales_destination,
                            commission, is_shipped, created_at, brand_name, item_condition, additional_photos,
                            sale_type, model_number, supplier_detail, id_document_path, consent_form_path,
                            updated_by, updated_at, notes, show_in_proxy_service, kaika_product_code)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        user_id,
                        item.get('purchase_date'),
                        item.get('photo_path'),
                        item.get('product_name'),
                        item.get('store_name'),
                        item.get('purchase_price', 0),
                        item.get('payment_method'),
                        item.get('listing_price', 0),
                        item.get('expected_shipping', 0),
                        item.get('expected_commission', 0),
                        1 if item.get('is_listed') else 0,
                        item.get('listing_date'),
                        item.get('sale_date'),
                        item.get('sale_price', 0),
                        item.get('shipping_cost', 0),
                        item.get('sales_destination'),
                        item.get('commission', 0),
                        1 if item.get('is_shipped') else 0,
                        item.get('created_at'),
                        item.get('brand_name'),
                        item.get('item_condition'),
                        item.get('additional_photos'),
                        item.get('sale_type', 'normal'),
                        item.get('model_number'),
                        item.get('supplier_detail'),
                        item.get('id_document_path'),
                        item.get('consent_form_path'),
                        resolve_user_id(item.get('updated_by')) if item.get('updated_by') else None,  # 外部キー制約対応
                        item.get('updated_at'),
                        item.get('notes'),
                        1 if item.get('show_in_proxy_service') else 0,
                        item.get('kaika_product_code')
                    ))
                imported_counts['merchandise'] += 1
                print(f"DEBUG: Successfully imported: {item.get('product_name')}", flush=True)
            except Exception as e:
                if not DATABASE_URL: conn.rollback()  # SQLiteの場合のみrollback
                import traceback
                import sys
                print(f"Merchandise import error: {e}", flush=True)
                print(f"Item data: {item.get('product_name')} (id={item.get('id')}, user_id={item.get('user_id')})", flush=True)
                traceback.print_exc()
                sys.stdout.flush()
                sys.stderr.flush()
                if 'errors' not in imported_counts:
                    imported_counts['errors'] = []
                imported_counts['errors'].append(f"{item.get('product_name')}: {str(e)}")
        
        # 顧客をインポート
        for customer in backup_data.get('customers', []):
            try:
                # user_idを解決（旧環境のIDを新環境のIDにマッピング）
                old_user_id = customer.get('user_id')
                if 'username' in backup_data and 'users' not in backup_data:
                    user_id = current_user.id
                else:
                    user_id = resolve_user_id(old_user_id)
                
                if DATABASE_URL:
                    cur.execute('''
                        INSERT INTO customers (user_id, name, email, phone, address, total_purchase, purchase_count, notes, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (
                        user_id,
                        customer.get('name'),
                        customer.get('email'),
                        customer.get('phone'),
                        customer.get('address'),
                        customer.get('total_purchase', 0),
                        customer.get('purchase_count', 0),
                        customer.get('notes'),
                        customer.get('created_at')
                    ))
                else:
                    cur.execute('''
                        INSERT INTO customers (user_id, name, email, phone, address, total_purchase, purchase_count, notes, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        user_id,
                        customer.get('name'),
                        customer.get('email'),
                        customer.get('phone'),
                        customer.get('address'),
                        customer.get('total_purchase', 0),
                        customer.get('purchase_count', 0),
                        customer.get('notes'),
                        customer.get('created_at')
                    ))
                imported_counts['customers'] += 1
            except Exception as e:
                if not DATABASE_URL: conn.rollback()  # SQLiteの場合のみrollback
                print(f"Customer import error: {e}")
        
        # 問い合わせをインポート（v3.1追加）
        imported_counts['inquiries'] = 0
        inquiry_id_map = {}  # 旧ID → 新ID のマッピング
        for inquiry in backup_data.get('inquiries', []):
            try:
                old_inquiry_id = inquiry.get('id')
                user_id = resolve_user_id(inquiry.get('user_id'))
                
                if DATABASE_URL:
                    cur.execute('''
                        INSERT INTO inquiries (user_id, category, title, content, image_path, status, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    ''', (
                        user_id,
                        inquiry.get('category', 'general'),
                        inquiry.get('title'),
                        inquiry.get('content'),
                        inquiry.get('image_path'),
                        inquiry.get('status', 'new'),
                        inquiry.get('created_at'),
                        inquiry.get('updated_at')
                    ))
                    new_id = cur.fetchone()[0]
                else:
                    cur.execute('''
                        INSERT INTO inquiries (user_id, category, title, content, image_path, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        user_id,
                        inquiry.get('category', 'general'),
                        inquiry.get('title'),
                        inquiry.get('content'),
                        inquiry.get('image_path'),
                        inquiry.get('status', 'new'),
                        inquiry.get('created_at'),
                        inquiry.get('updated_at')
                    ))
                    new_id = cur.lastrowid
                
                if old_inquiry_id is not None:
                    inquiry_id_map[old_inquiry_id] = new_id
                imported_counts['inquiries'] += 1
            except Exception as e:
                if not DATABASE_URL: conn.rollback()  # SQLiteの場合のみrollback
                print(f"Inquiry import error: {e}")
        
        # 問い合わせ返信をインポート（v3.1追加）
        imported_counts['inquiry_replies'] = 0
        for reply in backup_data.get('inquiry_replies', []):
            try:
                old_inquiry_id = reply.get('inquiry_id')
                new_inquiry_id = inquiry_id_map.get(old_inquiry_id)
                if new_inquiry_id is None:
                    continue  # 対応する問い合わせがない場合はスキップ
                
                user_id = resolve_user_id(reply.get('user_id'))
                
                if DATABASE_URL:
                    cur.execute('''
                        INSERT INTO inquiry_replies (inquiry_id, user_id, content, is_admin_reply, created_at)
                        VALUES (%s, %s, %s, %s, %s)
                    ''', (
                        new_inquiry_id,
                        user_id,
                        reply.get('content'),
                        reply.get('is_admin_reply', False),
                        reply.get('created_at')
                    ))
                else:
                    cur.execute('''
                        INSERT INTO inquiry_replies (inquiry_id, user_id, content, is_admin_reply, created_at)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        new_inquiry_id,
                        user_id,
                        reply.get('content'),
                        1 if reply.get('is_admin_reply') else 0,
                        reply.get('created_at')
                    ))
                imported_counts['inquiry_replies'] += 1
            except Exception as e:
                if not DATABASE_URL: conn.rollback()  # SQLiteの場合のみrollback
                print(f"Inquiry reply import error: {e}")
        
        # 管理者用買取承諾書（法人版）をインポート（v3.1追加）
        imported_counts['admin_kaitori_shoudaku'] = 0
        admin_kaitori_id_map = {}  # 旧ID → 新ID のマッピング
        for kaitori in backup_data.get('admin_kaitori_shoudaku', []):
            try:
                old_kaitori_id = kaitori.get('id')
                admin_id = resolve_user_id(kaitori.get('admin_id'))
                
                if DATABASE_URL:
                    cur.execute('''
                        INSERT INTO admin_kaitori_shoudaku (document_no, admin_id, company_name, company_address, company_phone,
                            contact_name, issue_date, subtotal, tax_amount, total_amount, tax_rate, payment_method, bank_info,
                            notes, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                    ''', (
                        kaitori.get('document_no'),
                        admin_id,
                        kaitori.get('company_name'),
                        kaitori.get('company_address'),
                        kaitori.get('company_phone'),
                        kaitori.get('contact_name'),
                        kaitori.get('issue_date'),
                        kaitori.get('subtotal', 0),
                        kaitori.get('tax_amount', 0),
                        kaitori.get('total_amount', 0),
                        kaitori.get('tax_rate', 10.0),
                        kaitori.get('payment_method'),
                        kaitori.get('bank_info'),
                        kaitori.get('notes'),
                        kaitori.get('created_at'),
                        kaitori.get('updated_at')
                    ))
                    new_id = cur.fetchone()[0]
                else:
                    cur.execute('''
                        INSERT INTO admin_kaitori_shoudaku (document_no, admin_id, company_name, company_address, company_phone,
                            contact_name, issue_date, subtotal, tax_amount, total_amount, tax_rate, payment_method, bank_info,
                            notes, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        kaitori.get('document_no'),
                        admin_id,
                        kaitori.get('company_name'),
                        kaitori.get('company_address'),
                        kaitori.get('company_phone'),
                        kaitori.get('contact_name'),
                        kaitori.get('issue_date'),
                        kaitori.get('subtotal', 0),
                        kaitori.get('tax_amount', 0),
                        kaitori.get('total_amount', 0),
                        kaitori.get('tax_rate', 10.0),
                        kaitori.get('payment_method'),
                        kaitori.get('bank_info'),
                        kaitori.get('notes'),
                        kaitori.get('created_at'),
                        kaitori.get('updated_at')
                    ))
                    new_id = cur.lastrowid
                
                if old_kaitori_id is not None:
                    admin_kaitori_id_map[old_kaitori_id] = new_id
                imported_counts['admin_kaitori_shoudaku'] += 1
            except Exception as e:
                if not DATABASE_URL: conn.rollback()  # SQLiteの場合のみrollback
                print(f"Admin kaitori shoudaku import error: {e}")
        
        # 管理者用買取承諾書明細をインポート（v3.1追加）
        imported_counts['admin_kaitori_shoudaku_items'] = 0
        for item in backup_data.get('admin_kaitori_shoudaku_items', []):
            try:
                old_kaitori_id = item.get('kaitori_shoudaku_id')
                new_kaitori_id = admin_kaitori_id_map.get(old_kaitori_id)
                if new_kaitori_id is None:
                    continue  # 対応する買取承諾書がない場合はスキップ
                
                if DATABASE_URL:
                    cur.execute('''
                        INSERT INTO admin_kaitori_shoudaku_items (kaitori_shoudaku_id, item_no, product_name, brand_name,
                            condition, quantity, unit_price, amount, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (
                        new_kaitori_id,
                        item.get('item_no'),
                        item.get('product_name'),
                        item.get('brand_name'),
                        item.get('condition'),
                        item.get('quantity', 1),
                        item.get('unit_price', 0),
                        item.get('amount', 0),
                        item.get('notes')
                    ))
                else:
                    cur.execute('''
                        INSERT INTO admin_kaitori_shoudaku_items (kaitori_shoudaku_id, item_no, product_name, brand_name,
                            condition, quantity, unit_price, amount, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        new_kaitori_id,
                        item.get('item_no'),
                        item.get('product_name'),
                        item.get('brand_name'),
                        item.get('condition'),
                        item.get('quantity', 1),
                        item.get('unit_price', 0),
                        item.get('amount', 0),
                        item.get('notes')
                    ))
                imported_counts['admin_kaitori_shoudaku_items'] += 1
            except Exception as e:
                if not DATABASE_URL: conn.rollback()  # SQLiteの場合のみrollback
                print(f"Admin kaitori shoudaku item import error: {e}")
        
        # 処分申請をインポート（v3.1追加）
        imported_counts['item_disposal_requests'] = 0
        for disposal in backup_data.get('item_disposal_requests', []):
            try:
                user_id = resolve_user_id(disposal.get('user_id'))
                processed_by = resolve_user_id(disposal.get('processed_by')) if disposal.get('processed_by') else None
                
                if DATABASE_URL:
                    cur.execute('''
                        INSERT INTO item_disposal_requests (user_id, merchandise_id, disposal_type, reason, shipping_address,
                            shipping_name, shipping_phone, status, admin_note, created_at, processed_at, processed_by)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (
                        user_id,
                        disposal.get('merchandise_id'),
                        disposal.get('disposal_type'),
                        disposal.get('reason', 'overdue'),
                        disposal.get('shipping_address'),
                        disposal.get('shipping_name'),
                        disposal.get('shipping_phone'),
                        disposal.get('status', 'pending'),
                        disposal.get('admin_note'),
                        disposal.get('created_at'),
                        disposal.get('processed_at'),
                        processed_by
                    ))
                else:
                    cur.execute('''
                        INSERT INTO item_disposal_requests (user_id, merchandise_id, disposal_type, reason, shipping_address,
                            shipping_name, shipping_phone, status, admin_note, created_at, processed_at, processed_by)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        user_id,
                        disposal.get('merchandise_id'),
                        disposal.get('disposal_type'),
                        disposal.get('reason', 'overdue'),
                        disposal.get('shipping_address'),
                        disposal.get('shipping_name'),
                        disposal.get('shipping_phone'),
                        disposal.get('status', 'pending'),
                        disposal.get('admin_note'),
                        disposal.get('created_at'),
                        disposal.get('processed_at'),
                        processed_by
                    ))
                imported_counts['item_disposal_requests'] += 1
            except Exception as e:
                if not DATABASE_URL: conn.rollback()  # SQLiteの場合のみrollback
                print(f"Item disposal request import error: {e}")
        
        # 代行サービス設定をインポート（v3.2追加、v3.3でauction_name対応）
        imported_counts['proxy_service_settings'] = 0
        # 既存データを削除（ループの外で1回だけ実行）
        proxy_settings_list = backup_data.get('proxy_service_settings', [])
        if proxy_settings_list:
            if DATABASE_URL:
                cur.execute("DELETE FROM proxy_service_settings")
            else:
                cur.execute("DELETE FROM proxy_service_settings")
        
        for setting in proxy_settings_list:
            try:
                if DATABASE_URL:
                    cur.execute('''
                        INSERT INTO proxy_service_settings (is_public, page_title, page_description, start_datetime, end_datetime, sale_mode, auction_name, updated_by, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (
                        setting.get('is_public', False),
                        setting.get('page_title', '代行仕入れサービス'),
                        setting.get('page_description'),
                        setting.get('start_datetime'),
                        setting.get('end_datetime'),
                        setting.get('sale_mode', 'auction'),
                        setting.get('auction_name', 'オークション'),
                        current_user.id,
                        setting.get('updated_at')
                    ))
                else:
                    cur.execute('''
                        INSERT INTO proxy_service_settings (is_public, page_title, page_description, start_datetime, end_datetime, sale_mode, auction_name, updated_by, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        1 if setting.get('is_public') else 0,
                        setting.get('page_title', '代行仕入れサービス'),
                        setting.get('page_description'),
                        setting.get('start_datetime'),
                        setting.get('end_datetime'),
                        setting.get('sale_mode', 'auction'),
                        setting.get('auction_name', 'オークション'),
                        current_user.id,
                        setting.get('updated_at')
                    ))
                imported_counts['proxy_service_settings'] += 1
            except Exception as e:
                if not DATABASE_URL: conn.rollback()  # SQLiteの場合のみrollback
                print(f"Proxy service settings import error: {e}")
        
        # 代行サービス公開ユーザーをインポート（v3.2追加）
        imported_counts['proxy_service_users'] = 0
        if DATABASE_URL:
            cur.execute("DELETE FROM proxy_service_users")
        else:
            cur.execute("DELETE FROM proxy_service_users")
        for psu in backup_data.get('proxy_service_users', []):
            try:
                user_id = resolve_user_id(psu.get('user_id'))
                if DATABASE_URL:
                    cur.execute('''
                        INSERT INTO proxy_service_users (user_id, is_enabled, created_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (user_id) DO NOTHING
                    ''', (
                        user_id,
                        psu.get('is_enabled', True),
                        psu.get('created_at')
                    ))
                else:
                    cur.execute('''
                        INSERT OR IGNORE INTO proxy_service_users (user_id, is_enabled, created_at)
                        VALUES (?, ?, ?)
                    ''', (
                        user_id,
                        1 if psu.get('is_enabled') else 0,
                        psu.get('created_at')
                    ))
                imported_counts['proxy_service_users'] += 1
            except Exception as e:
                if not DATABASE_URL: conn.rollback()  # SQLiteの場合のみrollback
                print(f"Proxy service user import error: {e}")
        
        # 代行サービス入札履歴をインポート（v3.2追加）
        imported_counts['proxy_service_bids'] = 0
        for bid in backup_data.get('proxy_service_bids', []):
            try:
                user_id = resolve_user_id(bid.get('user_id')) if bid.get('user_id') else None
                if DATABASE_URL:
                    cur.execute('''
                        INSERT INTO proxy_service_bids (merchandise_id, user_id, bidder_name, bid_amount, created_at)
                        VALUES (%s, %s, %s, %s, %s)
                    ''', (
                        bid.get('merchandise_id'),
                        user_id,
                        bid.get('bidder_name'),
                        bid.get('bid_amount'),
                        bid.get('created_at')
                    ))
                else:
                    cur.execute('''
                        INSERT INTO proxy_service_bids (merchandise_id, user_id, bidder_name, bid_amount, created_at)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        bid.get('merchandise_id'),
                        user_id,
                        bid.get('bidder_name'),
                        bid.get('bid_amount'),
                        bid.get('created_at')
                    ))
                imported_counts['proxy_service_bids'] += 1
            except Exception as e:
                if not DATABASE_URL: conn.rollback()  # SQLiteの場合のみrollback
                print(f"Proxy service bid import error: {e}")
        
        # 販売代行申請をインポート（v3.2追加）
        imported_counts['sales_agency_requests'] = 0
        sales_agency_id_map = {}
        for sar in backup_data.get('sales_agency_requests', []):
            try:
                old_sar_id = sar.get('id')
                user_id = resolve_user_id(sar.get('user_id'))
                processed_by = resolve_user_id(sar.get('processed_by')) if sar.get('processed_by') else None
                
                if DATABASE_URL:
                    cur.execute('''
                        INSERT INTO sales_agency_requests (user_id, service_type, status, admin_note, created_at, processed_at, processed_by, result_notified)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                    ''', (
                        user_id,
                        sar.get('service_type'),
                        sar.get('status', 'pending'),
                        sar.get('admin_note'),
                        sar.get('created_at'),
                        sar.get('processed_at'),
                        processed_by,
                        sar.get('result_notified', 0)
                    ))
                    new_id = cur.fetchone()[0]
                else:
                    cur.execute('''
                        INSERT INTO sales_agency_requests (user_id, service_type, status, admin_note, created_at, processed_at, processed_by, result_notified)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        user_id,
                        sar.get('service_type'),
                        sar.get('status', 'pending'),
                        sar.get('admin_note'),
                        sar.get('created_at'),
                        sar.get('processed_at'),
                        processed_by,
                        sar.get('result_notified', 0)
                    ))
                    new_id = cur.lastrowid
                
                if old_sar_id is not None:
                    sales_agency_id_map[old_sar_id] = new_id
                imported_counts['sales_agency_requests'] += 1
            except Exception as e:
                if not DATABASE_URL: conn.rollback()  # SQLiteの場合のみrollback
                print(f"Sales agency request import error: {e}")
        
        # 販売代行申請商品をインポート（v3.2追加）
        imported_counts['sales_agency_request_items'] = 0
        for item in backup_data.get('sales_agency_request_items', []):
            try:
                old_request_id = item.get('request_id')
                new_request_id = sales_agency_id_map.get(old_request_id)
                if new_request_id is None:
                    continue  # 対応する申請がない場合はスキップ
                
                if DATABASE_URL:
                    cur.execute('''
                        INSERT INTO sales_agency_request_items (request_id, merchandise_id)
                        VALUES (%s, %s)
                    ''', (
                        new_request_id,
                        item.get('merchandise_id')
                    ))
                else:
                    cur.execute('''
                        INSERT INTO sales_agency_request_items (request_id, merchandise_id)
                        VALUES (?, ?)
                    ''', (
                        new_request_id,
                        item.get('merchandise_id')
                    ))
                imported_counts['sales_agency_request_items'] += 1
            except Exception as e:
                if not DATABASE_URL:  # SQLiteの場合のみrollback
                    conn.rollback()
                print(f"Sales agency request item import error: {e}")
        
        # SQLiteの場合のみcommit（PostgreSQLはautocommitモード）
        if not DATABASE_URL:
            conn.commit()
        error_count = len(imported_counts.get('errors', []))
        msg = f"インポート完了: ユーザー {imported_counts['users']}件, 商品 {imported_counts['merchandise']}件, 顧客 {imported_counts['customers']}件"
        if imported_counts.get('inquiries', 0) > 0:
            msg += f", 問い合わせ {imported_counts['inquiries']}件"
        if imported_counts.get('admin_kaitori_shoudaku', 0) > 0:
            msg += f", 買取承諾書(法人) {imported_counts['admin_kaitori_shoudaku']}件"
        if imported_counts.get('item_disposal_requests', 0) > 0:
            msg += f", 処分申請 {imported_counts['item_disposal_requests']}件"
        if imported_counts.get('proxy_service_bids', 0) > 0:
            msg += f", 代行サービス入札 {imported_counts['proxy_service_bids']}件"
        if imported_counts.get('sales_agency_requests', 0) > 0:
            msg += f", 販売代行申請 {imported_counts['sales_agency_requests']}件"
        if error_count > 0:
            msg += f" (エラー {error_count}件)"
            flash(msg, 'warning')
            # エラー詳細をflashとログに出力
            errors = imported_counts.get('errors', [])
            if errors:
                error_summary = '; '.join(errors[:3])  # 最初の3件のみ表示
                if len(errors) > 3:
                    error_summary += f' ... 他{len(errors)-3}件'
                flash(f"エラー詳細: {error_summary}", 'error')
            print(f"Import errors: {errors}", flush=True)
        else:
            flash(msg, 'success')
        
    except Exception as e:
        if not DATABASE_URL:  # SQLiteの場合のみrollback
            conn.rollback()
        import traceback
        traceback.print_exc()
        flash(f'インポートエラー: {str(e)}', 'error')
    finally:
        cur.close()
        conn.close()
    
    return redirect(url_for('admin_backup'))

@app.route('/backup/import_user', methods=['POST'])
@login_required
def import_user_backup():
    """自分のデータをJSON/ZIPファイルからインポート"""
    if 'backup_file' not in request.files:
        flash('ファイルが選択されていません', 'error')
        return redirect(url_for('index'))
    
    file = request.files['backup_file']
    if file.filename == '':
        flash('ファイルが選択されていません', 'error')
        return redirect(url_for('index'))
    
    backup_data = None
    
    # ZIPファイルの場合
    if file.filename.endswith('.zip'):
        try:
            with zipfile.ZipFile(file, 'r') as zip_file:
                if 'backup_data.json' in zip_file.namelist():
                    with zip_file.open('backup_data.json') as json_file:
                        backup_data = json.load(json_file)
                
                # 画像ファイルを抽出
                for name in zip_file.namelist():
                    if name.startswith('images/') and not name.endswith('/'):
                        filename = os.path.basename(name)
                        image_data = zip_file.read(name)
                        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                        with open(save_path, 'wb') as f:
                            f.write(image_data)
        except zipfile.BadZipFile:
            flash('無効なZIPファイルです', 'error')
            return redirect(url_for('index'))
    # JSONファイルの場合
    elif file.filename.endswith('.json'):
        try:
            backup_data = json.load(file)
        except json.JSONDecodeError:
            flash('無効なJSONファイルです', 'error')
            return redirect(url_for('index'))
    else:
        flash('JSONまたはZIPファイルを選択してください', 'error')
        return redirect(url_for('index'))
    
    if not backup_data:
        flash('バックアップデータが見つかりません', 'error')
        return redirect(url_for('index'))
    
    conn = get_db()
    # PostgreSQLの場合はautocommitモードを有効化（各INSERT文を即座にコミット）
    if DATABASE_URL:
        conn.autocommit = True
    cur = conn.cursor()
    
    try:
        imported_counts = {'merchandise': 0, 'customers': 0}
        
        # 商品をインポート
        for item in backup_data.get('merchandise', []):
            try:
                if DATABASE_URL:
                    cur.execute('''
                        INSERT INTO merchandise (user_id, purchase_date, photo_path, product_name, store_name,
                            purchase_price, payment_method, listing_price, expected_shipping, expected_commission,
                            is_listed, listing_date, sale_date, sale_price, shipping_cost, sales_destination,
                            commission, is_shipped, created_at, brand_name, item_condition, additional_photos,
                            sale_type, model_number, supplier_detail, id_document_path, consent_form_path,
                            updated_by, updated_at, notes, show_in_proxy_service, kaika_product_code)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (
                        current_user.id,
                        item.get('purchase_date'),
                        item.get('photo_path'),
                        item.get('product_name'),
                        item.get('store_name'),
                        item.get('purchase_price', 0),
                        item.get('payment_method'),
                        item.get('listing_price', 0),
                        item.get('expected_shipping', 0),
                        item.get('expected_commission', 0),
                        bool(item.get('is_listed', False)),  # PostgreSQLのboolean型に変換
                        item.get('listing_date'),
                        item.get('sale_date'),
                        item.get('sale_price', 0),
                        item.get('shipping_cost', 0),
                        item.get('sales_destination'),
                        item.get('commission', 0),
                        bool(item.get('is_shipped', False)),  # PostgreSQLのboolean型に変換
                        item.get('created_at'),
                        item.get('brand_name'),
                        item.get('item_condition'),
                        item.get('additional_photos'),
                        item.get('sale_type', 'normal'),
                        item.get('model_number'),
                        item.get('supplier_detail'),
                        item.get('id_document_path'),
                        item.get('consent_form_path'),
                        current_user.id if item.get('updated_by') else None,  # ユーザー個別インポートでは現在のユーザーID
                        item.get('updated_at'),
                        item.get('notes'),
                        bool(item.get('show_in_proxy_service', False)),  # PostgreSQLのboolean型に変換
                        item.get('kaika_product_code')
                    ))
                else:
                    cur.execute('''
                        INSERT INTO merchandise (user_id, purchase_date, photo_path, product_name, store_name,
                            purchase_price, payment_method, listing_price, expected_shipping, expected_commission,
                            is_listed, listing_date, sale_date, sale_price, shipping_cost, sales_destination,
                            commission, is_shipped, created_at, brand_name, item_condition, additional_photos,
                            sale_type, model_number, supplier_detail, id_document_path, consent_form_path,
                            updated_by, updated_at, notes, show_in_proxy_service, kaika_product_code)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        current_user.id,
                        item.get('purchase_date'),
                        item.get('photo_path'),
                        item.get('product_name'),
                        item.get('store_name'),
                        item.get('purchase_price', 0),
                        item.get('payment_method'),
                        item.get('listing_price', 0),
                        item.get('expected_shipping', 0),
                        item.get('expected_commission', 0),
                        1 if item.get('is_listed') else 0,
                        item.get('listing_date'),
                        item.get('sale_date'),
                        item.get('sale_price', 0),
                        item.get('shipping_cost', 0),
                        item.get('sales_destination'),
                        item.get('commission', 0),
                        1 if item.get('is_shipped') else 0,
                        item.get('created_at'),
                        item.get('brand_name'),
                        item.get('item_condition'),
                        item.get('additional_photos'),
                        item.get('sale_type', 'normal'),
                        item.get('model_number'),
                        item.get('supplier_detail'),
                        item.get('id_document_path'),
                        item.get('consent_form_path'),
                        current_user.id if item.get('updated_by') else None,  # ユーザー個別インポートでは現在のユーザーID
                        item.get('updated_at'),
                        item.get('notes'),
                        1 if item.get('show_in_proxy_service') else 0,
                        item.get('kaika_product_code')
                    ))
                imported_counts['merchandise'] += 1
                print(f"DEBUG: Successfully imported (user): {item.get('product_name')}", flush=True)
            except Exception as e:
                if not DATABASE_URL: conn.rollback()  # SQLiteの場合のみrollback
                import traceback
                import sys
                print(f"Merchandise import error (user): {e}", flush=True)
                print(f"Item data: {item.get('product_name')} (id={item.get('id')}, user_id={item.get('user_id')})", flush=True)
                traceback.print_exc()
                sys.stdout.flush()
                sys.stderr.flush()
                if 'errors' not in imported_counts:
                    imported_counts['errors'] = []
                imported_counts['errors'].append(f"{item.get('product_name')}: {str(e)}")
        
        # 顧客をインポート
        for customer in backup_data.get('customers', []):
            try:
                if DATABASE_URL:
                    cur.execute('''
                        INSERT INTO customers (user_id, name, email, phone, address, total_purchase, purchase_count, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (
                        current_user.id,
                        customer.get('name'),
                        customer.get('email'),
                        customer.get('phone'),
                        customer.get('address'),
                        customer.get('total_purchase', 0),
                        customer.get('purchase_count', 0),
                        customer.get('notes')
                    ))
                else:
                    cur.execute('''
                        INSERT INTO customers (user_id, name, email, phone, address, total_purchase, purchase_count, notes)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        current_user.id,
                        customer.get('name'),
                        customer.get('email'),
                        customer.get('phone'),
                        customer.get('address'),
                        customer.get('total_purchase', 0),
                        customer.get('purchase_count', 0),
                        customer.get('notes')
                    ))
                imported_counts['customers'] += 1
            except Exception as e:
                if not DATABASE_URL:  # SQLiteの場合のみrollback
                    conn.rollback()
                print(f"Customer import error: {e}")
        
        # SQLiteの場合のみcommit（PostgreSQLはautocommitモード）
        if not DATABASE_URL:
            conn.commit()
        error_count = len(imported_counts.get('errors', []))
        msg = f"インポート完了: 商品 {imported_counts['merchandise']}件, 顧客 {imported_counts['customers']}件"
        if error_count > 0:
            msg += f" (エラー {error_count}件)"
            flash(msg, 'warning')
            print(f"Import errors: {imported_counts.get('errors', [])}")
        else:
            flash(msg, 'success')
        
    except Exception as e:
        if not DATABASE_URL:  # SQLiteの場合のみrollback
            conn.rollback()
        import traceback
        traceback.print_exc()
        flash(f'インポートエラー: {str(e)}', 'error')
    finally:
        cur.close()
        conn.close()
    
    return redirect(url_for('index'))

# ===================
# 管理者商品管理ルート
# ===================

@app.route('/admin/items')
@login_required
@admin_required
def admin_items():
    """管理者商品一覧（管理者/オーナーの商品のみ）"""
    items = []
    users = []
    
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # 管理者/オーナーのユーザーIDを取得
            cur.execute("SELECT id, username, display_name, role FROM users WHERE role IN ('admin', 'owner')")
            admin_users = cur.fetchall()
            admin_user_ids = [u['id'] for u in admin_users]
            
            # 管理者/オーナーの商品のみ取得（user_id IS NULLの管理者商品も含む）
            if admin_user_ids:
                placeholders = ','.join(['%s'] * len(admin_user_ids))
                cur.execute(f"SELECT * FROM merchandise WHERE user_id IN ({placeholders}) OR user_id IS NULL ORDER BY created_at DESC", admin_user_ids)
            else:
                cur.execute("SELECT * FROM merchandise WHERE user_id IS NULL ORDER BY created_at DESC")
            items_raw = cur.fetchall()
            
            # 転送先ユーザー一覧（全ユーザー）
            cur.execute("SELECT id, username, display_name, role FROM users ORDER BY username")
            users = cur.fetchall()
            
            # ユーザー情報をマッピング
            user_map = {u['id']: u for u in users}
            items = []
            for item in items_raw:
                item_dict = dict(item)
                user_info = user_map.get(item_dict.get('user_id'))
                if user_info:
                    item_dict['owner_username'] = user_info.get('username')
                    item_dict['owner_display_name'] = user_info.get('display_name')
                    item_dict['owner_role'] = user_info.get('role')
                else:
                    item_dict['owner_username'] = None
                    item_dict['owner_display_name'] = None
                    item_dict['owner_role'] = None
                
                # 全画像リスト（メイン + 追加）
                item_dict['all_photos'] = []
                if item_dict.get('photo_path'):
                    item_dict['all_photos'].append(item_dict['photo_path'].replace('\\', '/'))
                if item_dict.get('additional_photos'):
                    try:
                        additional = json.loads(item_dict['additional_photos']) if isinstance(item_dict['additional_photos'], str) else item_dict['additional_photos']
                        item_dict['all_photos'].extend([p.replace('\\', '/') for p in additional])
                    except:
                        pass
                
                items.append(item_dict)
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            # 管理者/オーナーのユーザーIDを取得
            cur.execute("SELECT id, username, display_name, role FROM users WHERE role IN ('admin', 'owner')")
            admin_users = cur.fetchall()
            admin_user_ids = [u['id'] for u in admin_users]
            
            # 管理者/オーナーの商品のみ取得（user_id IS NULLの管理者商品も含む）
            if admin_user_ids:
                placeholders = ','.join(['?'] * len(admin_user_ids))
                cur.execute(f"SELECT * FROM merchandise WHERE user_id IN ({placeholders}) OR user_id IS NULL ORDER BY created_at DESC", admin_user_ids)
            else:
                cur.execute("SELECT * FROM merchandise WHERE user_id IS NULL ORDER BY created_at DESC")
            items_raw = cur.fetchall()
            
            cur.execute("SELECT id, username, display_name, role FROM users ORDER BY username")
            users = cur.fetchall()
            
            # ユーザー情報をマッピング
            user_map = {u['id']: dict(u) for u in users}
            items = []
            for item in items_raw:
                item_dict = dict(item)
                user_info = user_map.get(item_dict.get('user_id'))
                if user_info:
                    item_dict['owner_username'] = user_info.get('username')
                    item_dict['owner_display_name'] = user_info.get('display_name')
                    item_dict['owner_role'] = user_info.get('role')
                else:
                    item_dict['owner_username'] = None
                    item_dict['owner_display_name'] = None
                    item_dict['owner_role'] = None
                
                # 全画像リスト（メイン + 追加）
                item_dict['all_photos'] = []
                if item_dict.get('photo_path'):
                    item_dict['all_photos'].append(item_dict['photo_path'].replace('\\', '/'))
                if item_dict.get('additional_photos'):
                    try:
                        additional = json.loads(item_dict['additional_photos']) if isinstance(item_dict['additional_photos'], str) else item_dict['additional_photos']
                        item_dict['all_photos'].extend([p.replace('\\', '/') for p in additional])
                    except:
                        pass
                
                items.append(item_dict)
        
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Admin items error: {e}")
        import traceback
        traceback.print_exc()
        return render_template('admin/admin_items.html', items=[], users=[])
    
    # 利益計算と利益率の追加、最終更新者名の追加
    all_users_map = {u['id'] if isinstance(u, dict) else u['id']: (dict(u).get('display_name') or dict(u).get('username')) for u in users}
    for item in items:
        if item.get('photo_path'):
            item['photo_path'] = item['photo_path'].replace('\\', '/')
        if item.get('sale_date'):
            sale_price = item.get('sale_price', 0) or 0
            purchase_price = item.get('purchase_price', 0) or 0
            shipping_cost = item.get('shipping_cost', 0) or 0
            commission = item.get('commission', 0) or 0
            
            item['profit'] = calculate_profit(sale_price, purchase_price, shipping_cost, commission)
            
            # 利益率計算（利益 ÷ 仕入れ金額 × 100）
            if purchase_price > 0:
                item['profit_rate'] = round((item['profit'] / purchase_price) * 100, 1)
            else:
                item['profit_rate'] = 0
        
        # 最終更新者名を追加
        updated_by_id = item.get('updated_by')
        if updated_by_id:
            item['updated_by_name'] = all_users_map.get(updated_by_id, '不明')
        else:
            item['updated_by_name'] = '-'
        
        # 削除可能フラグを追加（オーナーは常にTrue、管理者は1日以内のみTrue）
        if current_user.is_owner():
            item['can_delete'] = True
        else:
            # 管理者の場合、登録から1日以内かチェック
            created_at = item.get('created_at')
            can_delete = False
            if created_at:
                if isinstance(created_at, str):
                    try:
                        created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                    except:
                        try:
                            created_at = datetime.strptime(created_at[:19], '%Y-%m-%dT%H:%M:%S')
                        except:
                            created_at = None
                if created_at:
                    diff = datetime.now() - created_at
                    can_delete = diff.days < 1
            item['can_delete'] = can_delete
    
    return render_template('admin/admin_items.html', 
                          items=items, 
                          users=[dict(u) for u in users])


@app.route('/admin/user-products')
@login_required
@admin_required
def admin_user_products():
    """ユーザーの商品一覧（一般ユーザーの商品のみ）"""
    items = []
    users = []
    
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # 一般ユーザーのIDを取得
            cur.execute("SELECT id, username, display_name, role FROM users WHERE role = 'user'")
            normal_users = cur.fetchall()
            normal_user_ids = [u['id'] for u in normal_users]
            
            # 一般ユーザーの商品のみ取得
            if normal_user_ids:
                placeholders = ','.join(['%s'] * len(normal_user_ids))
                cur.execute(f"SELECT * FROM merchandise WHERE user_id IN ({placeholders}) ORDER BY created_at DESC", normal_user_ids)
            else:
                cur.execute("SELECT * FROM merchandise WHERE 1=0")
            items_raw = cur.fetchall()
            
            # 転送先ユーザー一覧（全ユーザー）
            cur.execute("SELECT id, username, display_name, role FROM users ORDER BY username")
            users = cur.fetchall()
            
            # ユーザー情報をマッピング
            user_map = {u['id']: u for u in users}
            items = []
            for item in items_raw:
                item_dict = dict(item)
                user_info = user_map.get(item_dict.get('user_id'))
                if user_info:
                    item_dict['owner_username'] = user_info.get('username')
                    item_dict['owner_display_name'] = user_info.get('display_name')
                    item_dict['owner_role'] = user_info.get('role')
                else:
                    item_dict['owner_username'] = None
                    item_dict['owner_display_name'] = None
                    item_dict['owner_role'] = None
                
                # 全画像リスト（メイン + 追加）
                item_dict['all_photos'] = []
                if item_dict.get('photo_path'):
                    item_dict['all_photos'].append(item_dict['photo_path'].replace('\\', '/'))
                if item_dict.get('additional_photos'):
                    try:
                        additional = json.loads(item_dict['additional_photos']) if isinstance(item_dict['additional_photos'], str) else item_dict['additional_photos']
                        item_dict['all_photos'].extend([p.replace('\\', '/') for p in additional])
                    except:
                        pass
                
                items.append(item_dict)
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            # 一般ユーザーのIDを取得
            cur.execute("SELECT id, username, display_name, role FROM users WHERE role = 'user'")
            normal_users = cur.fetchall()
            normal_user_ids = [u['id'] for u in normal_users]
            
            # 一般ユーザーの商品のみ取得
            if normal_user_ids:
                placeholders = ','.join(['?'] * len(normal_user_ids))
                cur.execute(f"SELECT * FROM merchandise WHERE user_id IN ({placeholders}) ORDER BY created_at DESC", normal_user_ids)
            else:
                cur.execute("SELECT * FROM merchandise WHERE 1=0")
            items_raw = cur.fetchall()
            
            cur.execute("SELECT id, username, display_name, role FROM users ORDER BY username")
            users = cur.fetchall()
            
            # ユーザー情報をマッピング
            user_map = {u['id']: dict(u) for u in users}
            items = []
            for item in items_raw:
                item_dict = dict(item)
                user_info = user_map.get(item_dict.get('user_id'))
                if user_info:
                    item_dict['owner_username'] = user_info.get('username')
                    item_dict['owner_display_name'] = user_info.get('display_name')
                    item_dict['owner_role'] = user_info.get('role')
                else:
                    item_dict['owner_username'] = None
                    item_dict['owner_display_name'] = None
                    item_dict['owner_role'] = None
                
                # 全画像リスト（メイン + 追加）
                item_dict['all_photos'] = []
                if item_dict.get('photo_path'):
                    item_dict['all_photos'].append(item_dict['photo_path'].replace('\\', '/'))
                if item_dict.get('additional_photos'):
                    try:
                        additional = json.loads(item_dict['additional_photos']) if isinstance(item_dict['additional_photos'], str) else item_dict['additional_photos']
                        item_dict['all_photos'].extend([p.replace('\\', '/') for p in additional])
                    except:
                        pass
                
                items.append(item_dict)
        
        cur.close()
        conn.close()
    except Exception as e:
        print(f"User products error: {e}")
        import traceback
        traceback.print_exc()
        return render_template('admin/user_products.html', items=[], users=[])
    
    # 利益計算と利益率の追加、最終更新者名の追加
    all_users_map = {u['id'] if isinstance(u, dict) else u['id']: (dict(u).get('display_name') or dict(u).get('username')) for u in users}
    for item in items:
        if item.get('photo_path'):
            item['photo_path'] = item['photo_path'].replace('\\', '/')
        if item.get('sale_date'):
            sale_price = item.get('sale_price', 0) or 0
            purchase_price = item.get('purchase_price', 0) or 0
            shipping_cost = item.get('shipping_cost', 0) or 0
            commission = item.get('commission', 0) or 0
            
            item['profit'] = calculate_profit(sale_price, purchase_price, shipping_cost, commission)
            
            # 利益率計算（利益 ÷ 仕入れ金額 × 100）
            if purchase_price > 0:
                item['profit_rate'] = round((item['profit'] / purchase_price) * 100, 1)
            else:
                item['profit_rate'] = 0
        
        # 最終更新者名を追加
        updated_by_id = item.get('updated_by')
        if updated_by_id:
            item['updated_by_name'] = all_users_map.get(updated_by_id, '不明')
        else:
            item['updated_by_name'] = '-'
        
        # 削除可能フラグを追加（オーナーは常にTrue、管理者は1日以内のみTrue）
        if current_user.is_owner():
            item['can_delete'] = True
        else:
            # 管理者の場合、登録から1日以内かチェック
            created_at = item.get('created_at')
            can_delete = False
            if created_at:
                if isinstance(created_at, str):
                    try:
                        created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                    except:
                        try:
                            created_at = datetime.strptime(created_at[:19], '%Y-%m-%dT%H:%M:%S')
                        except:
                            created_at = None
                if created_at:
                    diff = datetime.now() - created_at
                    can_delete = diff.days < 1
            item['can_delete'] = can_delete
    
    return render_template('admin/user_products.html', 
                          items=items, 
                          users=[dict(u) for u in users])

@app.route('/admin/items/add', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_add_item():
    """管理者用商品登録（モードによりユーザー/管理者商品を分離）"""
    # モード取得: 'user' = ユーザー商品登録, 'admin' = 管理者商品登録
    mode = request.args.get('mode', 'admin')
    
    if request.method == 'POST':
        # フォームからモードを取得
        form_mode = request.form.get('mode', 'admin')
        
        # ユーザーモードの場合、ユーザー選択必須
        if form_mode == 'user':
            target_user_id_str = request.form.get('target_user_id', '')
            if not target_user_id_str:
                flash('ユーザーを選択してください', 'error')
                return redirect(url_for('admin_add_item', mode='user'))
            target_user_id = int(target_user_id_str)
        else:
            # 管理者モードの場合、ユーザーなし
            target_user_id = None
        
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            placeholder = '%s'
        else:
            cur = conn.cursor()
            placeholder = '?'
        
        # 画像処理
        photo_path = None
        if 'photo' in request.files:
            photo = request.files['photo']
            if photo and photo.filename:
                filename = secure_filename(f"{int(time.time())}_{photo.filename}")
                photo_path = os.path.join('uploads', filename).replace('\\', '/')
                photo.save(os.path.join(app.static_folder, photo_path))
        
        # 追加写真処理
        additional_photos = []
        if 'additional_photos' in request.files:
            files = request.files.getlist('additional_photos')
            print(f"[DEBUG admin_add_item] 追加画像ファイル数: {len(files)}")
            for idx, photo in enumerate(files[:19]):
                print(f"[DEBUG admin_add_item] ファイル{idx}: filename={photo.filename}")
                if photo and photo.filename:
                    filename = secure_filename(f"{int(time.time())}_{idx}_{photo.filename}")
                    path = os.path.join('uploads', filename).replace('\\', '/')
                    photo.save(os.path.join(app.static_folder, path))
                    additional_photos.append(path)
                    print(f"[DEBUG admin_add_item] 保存: {path}")
        
        # JSON形式で保存（他の処理と統一）
        additional_photos_json = json.dumps(additional_photos) if additional_photos else None
        print(f"[DEBUG admin_add_item] additional_photos_json: {additional_photos_json}")
        
        # 身分証処理
        id_document_path = None
        if 'id_document' in request.files:
            doc = request.files['id_document']
            if doc and doc.filename:
                filename = secure_filename(f"id_{int(time.time())}_{doc.filename}")
                id_document_path = os.path.join('uploads', 'documents', filename).replace('\\', '/')
                os.makedirs(os.path.join(app.static_folder, 'uploads', 'documents'), exist_ok=True)
                doc.save(os.path.join(app.static_folder, id_document_path))
        
        # 同意書処理
        consent_form_path = None
        if 'consent_form' in request.files:
            doc = request.files['consent_form']
            if doc and doc.filename:
                filename = secure_filename(f"consent_{int(time.time())}_{doc.filename}")
                consent_form_path = os.path.join('uploads', 'documents', filename).replace('\\', '/')
                os.makedirs(os.path.join(app.static_folder, 'uploads', 'documents'), exist_ok=True)
                doc.save(os.path.join(app.static_folder, consent_form_path))
        
        # ステータス処理
        item_status = request.form.get('item_status', 'unlisted')
        is_listed = item_status in ['listed', 'sold']
        sale_date = request.form.get('sale_date') if item_status == 'sold' else None
        
        try:
            cur.execute(f'''
                INSERT INTO merchandise (
                    user_id, purchase_date, photo_path, additional_photos, product_name, brand_name, 
                    model_number, item_condition, store_name, supplier_detail, 
                    id_document_path, consent_form_path,
                    purchase_price, payment_method, listing_price, expected_shipping, 
                    expected_commission, is_listed, listing_date, sale_date, sale_type, sale_price, 
                    shipping_cost, sales_destination, commission, is_shipped
                ) VALUES ({', '.join([placeholder] * 26)})
            ''', (
                target_user_id,
                request.form.get('purchase_date') or None,
                photo_path,
                additional_photos_json,
                request.form.get('product_name'),
                request.form.get('brand_name'),
                request.form.get('model_number'),
                request.form.get('item_condition'),
                request.form.get('store_name'),
                request.form.get('supplier_detail'),
                id_document_path,
                consent_form_path,
                int(request.form.get('purchase_price') or 0),
                request.form.get('payment_method'),
                int(request.form.get('listing_price') or 0),
                int(request.form.get('expected_shipping') or 0),
                int(request.form.get('expected_commission') or 0),
                is_listed,
                request.form.get('listing_date') or None,
                sale_date or None,
                request.form.get('sale_type') or 'normal',
                int(request.form.get('sale_price') or 0),
                int(request.form.get('shipping_cost') or 0),
                request.form.get('sales_destination'),
                int(request.form.get('commission') or 0),
                'is_shipped' in request.form
            ))
            conn.commit()
            
            if target_user_id:
                flash('商品を登録し、指定ユーザーに割り当てました', 'success')
            else:
                flash('商品を管理者商品として登録しました', 'success')
        except Exception as e:
            conn.rollback()
            flash(f'エラー: {str(e)}', 'error')
        finally:
            cur.close()
            conn.close()
        
        # 登録後のリダイレクト先を決定
        if target_user_id:
            return redirect(url_for('admin_user_products'))
        else:
            return redirect(url_for('admin_items'))
    
    # GETリクエスト：ユーザー一覧を取得（ユーザーモードの場合のみ必要）
    users = []
    if mode == 'user':
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT id, username, display_name, role FROM users WHERE role = 'user' ORDER BY display_name, username")
            users = [dict(u) for u in cur.fetchall()]
        else:
            cur = conn.cursor()
            cur.execute("SELECT id, username, display_name, role FROM users WHERE role = 'user' ORDER BY display_name, username")
            users = [dict(zip(['id', 'username', 'display_name', 'role'], row)) for row in cur.fetchall()]
        cur.close()
        conn.close()
    
    # URLパラメータからデフォルトのユーザーIDを取得
    default_user_id = request.args.get('user_id', '')
    
    return render_template('admin/item_form.html', item=None, users=users, default_user_id=default_user_id, mode=mode)

@app.route('/admin/items/<int:id>/transfer', methods=['POST'])
@login_required
@admin_required
def admin_transfer_item(id):
    """商品を指定ユーザーに転送"""
    target_user_id = request.form.get('target_user_id')
    
    if not target_user_id:
        flash('転送先ユーザーを選択してください', 'error')
        return redirect(url_for('admin_items'))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # 商品の存在確認
        cur.execute("SELECT * FROM merchandise WHERE id = %s", (id,))
        item = cur.fetchone()
        
        if not item:
            flash('商品が見つかりません', 'error')
            cur.close()
            conn.close()
            return redirect(url_for('admin_items'))
        
        # 転送先ユーザーの確認
        cur.execute("SELECT * FROM users WHERE id = %s", (target_user_id,))
        target_user = cur.fetchone()
        
        if not target_user:
            flash('転送先ユーザーが見つかりません', 'error')
            cur.close()
            conn.close()
            return redirect(url_for('admin_items'))
        
        # 転送実行
        cur.execute("UPDATE merchandise SET user_id = %s WHERE id = %s", (target_user_id, id))
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM merchandise WHERE id = ?", (id,))
        item = cur.fetchone()
        
        if not item:
            flash('商品が見つかりません', 'error')
            cur.close()
            conn.close()
            return redirect(url_for('admin_items'))
        
        cur.execute("SELECT * FROM users WHERE id = ?", (target_user_id,))
        target_user = cur.fetchone()
        
        if not target_user:
            flash('転送先ユーザーが見つかりません', 'error')
            cur.close()
            conn.close()
            return redirect(url_for('admin_items'))
        
        cur.execute("UPDATE merchandise SET user_id = ? WHERE id = ?", (target_user_id, id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    target_name = target_user.get('display_name') or target_user.get('username') if isinstance(target_user, dict) else (target_user['display_name'] or target_user['username'])
    flash(f'商品を「{target_name}」に転送しました', 'success')
    return redirect(url_for('admin_items'))

@app.route('/admin/items/<int:id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_item(id):
    """管理者による商品削除
    - オーナー: いつでも削除可能
    - 管理者: 商品登録から1日以内のみ削除可能
    """
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM merchandise WHERE id = %s", (id,))
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM merchandise WHERE id = ?", (id,))
    
    item = cur.fetchone()
    if not item:
        cur.close()
        conn.close()
        flash('商品が見つかりません', 'error')
        return redirect(request.referrer or url_for('admin_user_products'))
    
    item_dict = dict(item)
    product_name = item_dict.get('product_name', '不明')
    
    # オーナーでない場合（管理者の場合）、登録から1日以内かチェック
    if not current_user.is_owner():
        created_at = item_dict.get('created_at')
        if created_at:
            # 文字列の場合はdatetimeに変換
            if isinstance(created_at, str):
                try:
                    created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                except:
                    try:
                        created_at = datetime.strptime(created_at[:19], '%Y-%m-%dT%H:%M:%S')
                    except:
                        created_at = None
            
            if created_at:
                now = datetime.now()
                diff = now - created_at
                if diff.days >= 1:
                    cur.close()
                    conn.close()
                    flash('登録から1日以上経過した商品は削除できません', 'error')
                    return redirect(request.referrer or url_for('admin_user_products'))
    
    # 削除実行
    if DATABASE_URL:
        cur.execute("DELETE FROM merchandise WHERE id = %s", (id,))
    else:
        cur.execute("DELETE FROM merchandise WHERE id = ?", (id,))
    
    conn.commit()
    cur.close()
    conn.close()
    flash(f'商品「{product_name}」を削除しました', 'info')
    return redirect(request.referrer or url_for('admin_user_products'))

@app.route('/admin/items/transfer-bulk', methods=['POST'])
@login_required
@admin_required
def admin_transfer_items_bulk():
    """複数商品を一括転送"""
    item_ids = request.form.getlist('item_ids')
    target_user_id = request.form.get('target_user_id')
    
    if not item_ids or not target_user_id:
        flash('商品と転送先ユーザーを選択してください', 'error')
        return redirect(url_for('admin_items'))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE id = %s", (target_user_id,))
        target_user = cur.fetchone()
        
        if not target_user:
            flash('転送先ユーザーが見つかりません', 'error')
            cur.close()
            conn.close()
            return redirect(url_for('admin_items'))
        
        for item_id in item_ids:
            cur.execute("UPDATE merchandise SET user_id = %s WHERE id = %s", (target_user_id, item_id))
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = ?", (target_user_id,))
        target_user = cur.fetchone()
        
        if not target_user:
            flash('転送先ユーザーが見つかりません', 'error')
            cur.close()
            conn.close()
            return redirect(url_for('admin_items'))
        
        for item_id in item_ids:
            cur.execute("UPDATE merchandise SET user_id = ? WHERE id = ?", (target_user_id, item_id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    target_name = target_user.get('display_name') or target_user.get('username') if isinstance(target_user, dict) else (target_user['display_name'] or target_user['username'])
    flash(f'{len(item_ids)}件の商品を「{target_name}」に転送しました', 'success')
    return redirect(url_for('admin_items'))

# ===================
# お知らせ管理ルート（管理者用）
# ===================

@app.route('/admin/announcements')
@login_required
@permission_required('announcements')
def admin_announcements():
    """お知らせ一覧"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT a.*, u.username as author_name 
            FROM announcements a 
            LEFT JOIN users u ON a.created_by = u.id 
            ORDER BY a.created_at DESC
        """)
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT a.*, u.username as author_name 
            FROM announcements a 
            LEFT JOIN users u ON a.created_by = u.id 
            ORDER BY a.created_at DESC
        """)
    
    announcements = cur.fetchall()
    cur.close()
    conn.close()
    
    return render_template('admin/announcements.html', announcements=[dict(a) for a in announcements])

@app.route('/admin/announcements/add', methods=['GET', 'POST'])
@login_required
@permission_required('announcements')
def admin_add_announcement():
    """お知らせ追加"""
    if request.method == 'POST':
        title = request.form.get('title')
        content = request.form.get('content')
        announcement_type = request.form.get('announcement_type', 'info')
        is_active = 'is_active' in request.form
        publish_at = request.form.get('publish_at') or None
        expire_at = request.form.get('expire_at') or None
        
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO announcements (title, content, announcement_type, is_active, publish_at, expire_at, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (title, content, announcement_type, is_active, publish_at, expire_at, current_user.id))
        else:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO announcements (title, content, announcement_type, is_active, publish_at, expire_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (title, content, announcement_type, 1 if is_active else 0, publish_at, expire_at, current_user.id))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('お知らせを追加しました', 'success')
        return redirect(url_for('admin_announcements'))
    
    return render_template('admin/announcement_form.html', announcement=None)

@app.route('/admin/announcements/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@permission_required('announcements')
def admin_edit_announcement(id):
    """お知らせ編集"""
    conn = get_db()
    
    if request.method == 'POST':
        title = request.form.get('title')
        content = request.form.get('content')
        announcement_type = request.form.get('announcement_type', 'info')
        is_active = 'is_active' in request.form
        publish_at = request.form.get('publish_at') or None
        expire_at = request.form.get('expire_at') or None
        
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute("""
                UPDATE announcements SET title=%s, content=%s, announcement_type=%s, is_active=%s, publish_at=%s, expire_at=%s
                WHERE id=%s
            """, (title, content, announcement_type, is_active, publish_at, expire_at, id))
        else:
            cur = conn.cursor()
            cur.execute("""
                UPDATE announcements SET title=?, content=?, announcement_type=?, is_active=?, publish_at=?, expire_at=?
                WHERE id=?
            """, (title, content, announcement_type, 1 if is_active else 0, publish_at, expire_at, id))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('お知らせを更新しました', 'success')
        return redirect(url_for('admin_announcements'))
    
    # GETリクエスト時はお知らせを取得
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM announcements WHERE id = %s", (id,))
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM announcements WHERE id = ?", (id,))
    
    announcement = cur.fetchone()
    cur.close()
    conn.close()
    
    if not announcement:
        flash('お知らせが見つかりません', 'error')
        return redirect(url_for('admin_announcements'))
    
    return render_template('admin/announcement_form.html', announcement=dict(announcement))

@app.route('/admin/announcements/delete/<int:id>')
@login_required
@permission_required('announcements')
def admin_delete_announcement(id):
    """お知らせ削除"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("DELETE FROM announcements WHERE id = %s", (id,))
    else:
        cur = conn.cursor()
        cur.execute("DELETE FROM announcements WHERE id = ?", (id,))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('お知らせを削除しました', 'success')
    return redirect(url_for('admin_announcements'))

@app.route('/admin/announcements/toggle/<int:id>')
@login_required
@permission_required('announcements')
def admin_toggle_announcement(id):
    """お知らせの有効/無効を切り替え"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("UPDATE announcements SET is_active = NOT is_active WHERE id = %s", (id,))
    else:
        cur = conn.cursor()
        cur.execute("UPDATE announcements SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?", (id,))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('お知らせの状態を更新しました', 'success')
    return redirect(url_for('admin_announcements'))

# ===================
# 精算書管理（管理者用）
# ===================

def generate_document_no():
    """精算書番号を生成"""
    now = datetime.now()
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT COUNT(*) as count FROM shikiriosho WHERE issue_date >= %s", 
                   (now.strftime('%Y-%m-01'),))
        result = cur.fetchone()
        count = (result['count'] if result else 0) + 1
    else:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM shikiriosho WHERE issue_date >= ?", 
                   (now.strftime('%Y-%m-01'),))
        result = cur.fetchone()
        count = (result[0] if result else 0) + 1
    cur.close()
    conn.close()
    return f"SK-{now.strftime('%Y%m')}-{count:04d}"

@app.route('/admin/shikiriosho')
@login_required
@permission_required('shikiriosho')
def admin_shikiriosho_list():
    """精算書一覧（管理者用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT s.*, u.display_name as recipient_display_name, u.username as recipient_username
            FROM shikiriosho s
            LEFT JOIN users u ON s.recipient_id = u.id
            ORDER BY s.created_at DESC
        """)
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.*, u.display_name as recipient_display_name, u.username as recipient_username
            FROM shikiriosho s
            LEFT JOIN users u ON s.recipient_id = u.id
            ORDER BY s.created_at DESC
        """)
    
    shikiriosho_list = [dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    
    return render_template('admin/shikiriosho_list.html', shikiriosho_list=shikiriosho_list)

@app.route('/admin/shikiriosho/add', methods=['GET', 'POST'])
@login_required
@permission_required('shikiriosho')
def admin_shikiriosho_add():
    """精算書作成（管理者用）"""
    conn = get_db()
    
    if request.method == 'POST':
        recipient_id = request.form.get('recipient_id')
        recipient_name = request.form.get('recipient_name', '')
        contact_name = request.form.get('contact_name', '')
        personal_number = request.form.get('personal_number', '')
        issue_date = request.form.get('issue_date')
        due_date = request.form.get('due_date') or None
        tax_rate = float(request.form.get('tax_rate', 10))
        notes = request.form.get('notes', '')
        status = request.form.get('status', 'draft')
        
        # 明細データ取得（精算書形式）
        item_names = request.form.getlist('item_name[]')
        product_dates = request.form.getlist('product_date[]')
        product_codes = request.form.getlist('product_code[]')
        amounts = request.form.getlist('amount[]')
        
        # 合計計算（税込金額を直接入力）
        total_amount = 0
        items = []
        for i, name in enumerate(item_names):
            if name.strip():
                amount = int(amounts[i]) if i < len(amounts) and amounts[i] else 0
                total_amount += amount
                items.append({
                    'item_no': i + 1,
                    'product_name': name,
                    'product_date': product_dates[i] if i < len(product_dates) and product_dates[i] else None,
                    'product_code': product_codes[i] if i < len(product_codes) else '',
                    'quantity': 1,
                    'unit_price': amount,
                    'amount': amount
                })
        
        # 内税方式（税込金額から税額を逆算）
        tax_amount = int(total_amount * tax_rate / (100 + tax_rate))
        subtotal = total_amount  # 税込金額をそのまま使用
        document_no = generate_document_no()
        
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                INSERT INTO shikiriosho 
                (document_no, sender_id, recipient_id, recipient_name, contact_name, personal_number,
                 issue_date, due_date, subtotal, tax_amount, total_amount, tax_rate, notes, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (document_no, current_user.id, recipient_id or None, recipient_name, contact_name, personal_number,
                  issue_date, due_date, subtotal, tax_amount, total_amount, tax_rate, notes, status))
            shikiriosho_id = cur.fetchone()['id']
            
            for item in items:
                cur.execute("""
                    INSERT INTO shikiriosho_items 
                    (shikiriosho_id, item_no, product_name, product_date, product_code, quantity, unit_price, amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (shikiriosho_id, item['item_no'], item['product_name'], item['product_date'],
                      item['product_code'], item['quantity'], item['unit_price'], item['amount']))
        else:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO shikiriosho 
                (document_no, sender_id, recipient_id, recipient_name, contact_name, personal_number,
                 issue_date, due_date, subtotal, tax_amount, total_amount, tax_rate, notes, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (document_no, current_user.id, recipient_id or None, recipient_name, contact_name, personal_number,
                  issue_date, due_date, subtotal, tax_amount, total_amount, tax_rate, notes, status))
            shikiriosho_id = cur.lastrowid
            
            for item in items:
                cur.execute("""
                    INSERT INTO shikiriosho_items 
                    (shikiriosho_id, item_no, product_name, product_date, product_code, quantity, unit_price, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (shikiriosho_id, item['item_no'], item['product_name'], item['product_date'],
                      item['product_code'], item['quantity'], item['unit_price'], item['amount']))
        
        conn.commit()
        cur.close()
        conn.close()
        
        if status == 'sent':
            flash(f'精算書 {document_no} を作成・送信しました', 'success')
        else:
            flash(f'精算書 {document_no} を下書き保存しました', 'success')
        return redirect(url_for('admin_shikiriosho_list'))
    
    # ユーザー一覧取得
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, username, display_name FROM users WHERE role != 'admin' ORDER BY display_name")
    else:
        cur = conn.cursor()
        cur.execute("SELECT id, username, display_name FROM users WHERE role != 'admin' ORDER BY display_name")
    
    users = [dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    
    return render_template('admin/shikiriosho_form.html', 
                          users=users, 
                          document_no=generate_document_no(),
                          today=datetime.now().strftime('%Y-%m-%d'))

@app.route('/admin/shikiriosho/edit/<int:id>', methods=['GET', 'POST'])
@login_required
@permission_required('shikiriosho')
def admin_shikiriosho_edit(id):
    """精算書編集（管理者用）"""
    conn = get_db()
    
    if request.method == 'POST':
        recipient_id = request.form.get('recipient_id')
        recipient_name = request.form.get('recipient_name', '')
        contact_name = request.form.get('contact_name', '')
        personal_number = request.form.get('personal_number', '')
        issue_date = request.form.get('issue_date')
        due_date = request.form.get('due_date') or None
        tax_rate = float(request.form.get('tax_rate', 10))
        notes = request.form.get('notes', '')
        status = request.form.get('status', 'draft')
        
        # 明細データ取得（精算書形式）
        item_names = request.form.getlist('item_name[]')
        product_dates = request.form.getlist('product_date[]')
        product_codes = request.form.getlist('product_code[]')
        amounts = request.form.getlist('amount[]')
        
        # 合計計算（税込金額を直接入力）
        total_amount = 0
        items = []
        for i, name in enumerate(item_names):
            if name.strip():
                amount = int(amounts[i]) if i < len(amounts) and amounts[i] else 0
                total_amount += amount
                items.append({
                    'item_no': i + 1,
                    'product_name': name,
                    'product_date': product_dates[i] if i < len(product_dates) and product_dates[i] else None,
                    'product_code': product_codes[i] if i < len(product_codes) else '',
                    'quantity': 1,
                    'unit_price': amount,
                    'amount': amount
                })
        
        # 内税方式（税込金額から税額を逆算）
        tax_amount = int(total_amount * tax_rate / (100 + tax_rate))
        subtotal = total_amount
        
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute("""
                UPDATE shikiriosho SET
                recipient_id = %s, recipient_name = %s, contact_name = %s, personal_number = %s,
                issue_date = %s, due_date = %s, subtotal = %s, tax_amount = %s, total_amount = %s, 
                tax_rate = %s, notes = %s, status = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (recipient_id or None, recipient_name, contact_name, personal_number,
                  issue_date, due_date, subtotal, tax_amount, total_amount, tax_rate, notes, status, id))
            
            cur.execute("DELETE FROM shikiriosho_items WHERE shikiriosho_id = %s", (id,))
            
            for item in items:
                cur.execute("""
                    INSERT INTO shikiriosho_items 
                    (shikiriosho_id, item_no, product_name, product_date, product_code, quantity, unit_price, amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (id, item['item_no'], item['product_name'], item['product_date'],
                      item['product_code'], item['quantity'], item['unit_price'], item['amount']))
        else:
            cur = conn.cursor()
            cur.execute("""
                UPDATE shikiriosho SET
                recipient_id = ?, recipient_name = ?, contact_name = ?, personal_number = ?,
                issue_date = ?, due_date = ?, subtotal = ?, tax_amount = ?, total_amount = ?, 
                tax_rate = ?, notes = ?, status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (recipient_id or None, recipient_name, contact_name, personal_number,
                  issue_date, due_date, subtotal, tax_amount, total_amount, tax_rate, notes, status, id))
            
            cur.execute("DELETE FROM shikiriosho_items WHERE shikiriosho_id = ?", (id,))
            
            for item in items:
                cur.execute("""
                    INSERT INTO shikiriosho_items 
                    (shikiriosho_id, item_no, product_name, product_date, product_code, quantity, unit_price, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (id, item['item_no'], item['product_name'], item['product_date'],
                      item['product_code'], item['quantity'], item['unit_price'], item['amount']))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('精算書を更新しました', 'success')
        return redirect(url_for('admin_shikiriosho_list'))
    
    # 精算書データ取得
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM shikiriosho WHERE id = %s", (id,))
        row = cur.fetchone()
        shikiriosho = dict(row) if row else None
        cur.execute("SELECT * FROM shikiriosho_items WHERE shikiriosho_id = %s ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT id, username, display_name FROM users WHERE role != 'admin' ORDER BY display_name")
        users = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM shikiriosho WHERE id = ?", (id,))
        row = cur.fetchone()
        shikiriosho = dict(row) if row else None
        cur.execute("SELECT * FROM shikiriosho_items WHERE shikiriosho_id = ? ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
        cur.execute("SELECT id, username, display_name FROM users WHERE role != 'admin' ORDER BY display_name")
        users = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    if not shikiriosho:
        flash('精算書が見つかりません', 'error')
        return redirect(url_for('admin_shikiriosho_list'))
    
    return render_template('admin/shikiriosho_form.html', 
                          shikiriosho=shikiriosho, 
                          items=items,
                          users=users,
                          today=datetime.now().strftime('%Y-%m-%d'))

@app.route('/admin/shikiriosho/delete/<int:id>')
@login_required
@permission_required('shikiriosho')
def admin_shikiriosho_delete(id):
    """精算書削除（管理者用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("DELETE FROM shikiriosho_items WHERE shikiriosho_id = %s", (id,))
        cur.execute("DELETE FROM shikiriosho WHERE id = %s", (id,))
    else:
        cur = conn.cursor()
        cur.execute("DELETE FROM shikiriosho_items WHERE shikiriosho_id = ?", (id,))
        cur.execute("DELETE FROM shikiriosho WHERE id = ?", (id,))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('精算書を削除しました', 'success')
    return redirect(url_for('admin_shikiriosho_list'))

@app.route('/admin/shikiriosho/send/<int:id>')
@login_required
@permission_required('shikiriosho')
def admin_shikiriosho_send(id):
    """精算書を送信（ステータスを'sent'に変更）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("UPDATE shikiriosho SET status = 'sent', updated_at = CURRENT_TIMESTAMP WHERE id = %s", (id,))
    else:
        cur = conn.cursor()
        cur.execute("UPDATE shikiriosho SET status = 'sent', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (id,))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('精算書を送信しました', 'success')
    return redirect(url_for('admin_shikiriosho_list'))

@app.route('/admin/shikiriosho/view/<int:id>')
@login_required
@permission_required('shikiriosho')
def admin_shikiriosho_view(id):
    """精算書詳細（管理者用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT s.*, u.display_name as recipient_display_name, u.username as recipient_username
            FROM shikiriosho s
            LEFT JOIN users u ON s.recipient_id = u.id
            WHERE s.id = %s
        """, (id,))
        shikiriosho = cur.fetchone()
        if shikiriosho:
            shikiriosho = dict(shikiriosho)
        cur.execute("SELECT * FROM shikiriosho_items WHERE shikiriosho_id = %s ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.*, u.display_name as recipient_display_name, u.username as recipient_username
            FROM shikiriosho s
            LEFT JOIN users u ON s.recipient_id = u.id
            WHERE s.id = ?
        """, (id,))
        shikiriosho = cur.fetchone()
        if shikiriosho:
            shikiriosho = dict(shikiriosho)
        cur.execute("SELECT * FROM shikiriosho_items WHERE shikiriosho_id = ? ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    if not shikiriosho:
        flash('精算書が見つかりません', 'error')
        return redirect(url_for('admin_shikiriosho_list'))
    
    return render_template('admin/shikiriosho_view.html', shikiriosho=shikiriosho, items=items)

# ===================
# 精算書（ユーザー用）
# ===================

# ===================
# 統合書類管理
# ===================

@app.route('/documents')
@login_required
def documents():
    """統合書類管理ページ"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 買取明細書
        cur.execute("""
            SELECT * FROM invoices WHERE sender_id = %s ORDER BY created_at DESC
        """, (current_user.id,))
        invoices = [dict(row) for row in cur.fetchall()]
        
        # ユーザー見積依頼書
        cur.execute("""
            SELECT * FROM user_mitsumori WHERE user_id = %s ORDER BY created_at DESC
        """, (current_user.id,))
        user_mitsumori_list = [dict(row) for row in cur.fetchall()]
        
        # ユーザー計算書
        cur.execute("""
            SELECT * FROM user_keisan WHERE user_id = %s ORDER BY created_at DESC
        """, (current_user.id,))
        user_keisan_list = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        
        # 買取明細書
        cur.execute("""
            SELECT * FROM invoices WHERE sender_id = ? ORDER BY created_at DESC
        """, (current_user.id,))
        invoices = [dict(row) for row in cur.fetchall()]
        
        # ユーザー見積依頼書
        cur.execute("""
            SELECT * FROM user_mitsumori WHERE user_id = ? ORDER BY created_at DESC
        """, (current_user.id,))
        user_mitsumori_list = [dict(row) for row in cur.fetchall()]
        
        # ユーザー計算書
        cur.execute("""
            SELECT * FROM user_keisan WHERE user_id = ? ORDER BY created_at DESC
        """, (current_user.id,))
        user_keisan_list = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return render_template('documents.html',
                          invoices=invoices,
                          user_mitsumori_list=user_mitsumori_list,
                          user_keisan_list=user_keisan_list)

@app.route('/service-document/create', methods=['POST'])
@login_required
def create_service_document():
    """サービス書類作成"""
    service_type = request.form.get('service_type')
    customer_name = request.form.get('customer_name')
    contact = request.form.get('contact', '')
    product_name = request.form.get('product_name')
    product_description = request.form.get('product_description', '')
    notes = request.form.get('notes', '')
    sales_amount = int(request.form.get('sales_amount') or 0)
    commission = int(request.form.get('commission') or 0)
    
    # サービス固有データをJSON化
    service_data = {
        'sales_amount': sales_amount
    }
    if service_type == 'photo_packing':
        service_data.update({
            'photo_count': request.form.get('photo_count', '10'),
            'packing_size': request.form.get('packing_size', 'medium')
        })
    elif service_type == 'wholesale':
        service_data.update({
            'quantity': request.form.get('quantity', '1'),
            'wholesale_price': request.form.get('wholesale_price', '0')
        })
    elif service_type == 'multi_listing':
        service_data.update({
            'listing_sites': request.form.getlist('listing_sites'),
            'listing_price': request.form.get('listing_price', '0')
        })
    elif service_type == 'auction':
        service_data.update({
            'min_bid': request.form.get('min_bid', '0'),
            'buy_now_price': request.form.get('buy_now_price', '0'),
            'auction_duration': request.form.get('auction_duration', '5')
        })
    
    # 書類番号生成
    doc_no = f"SD-{datetime.now().strftime('%Y%m%d%H%M%S')}-{current_user.id}"
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO service_documents 
            (document_no, user_id, service_type, customer_name, contact, product_name, 
             product_description, commission, total_amount, service_data, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (doc_no, current_user.id, service_type, customer_name, contact, product_name,
              product_description, commission, sales_amount, json.dumps(service_data), notes))
    else:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO service_documents 
            (document_no, user_id, service_type, customer_name, contact, product_name, 
             product_description, commission, total_amount, service_data, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (doc_no, current_user.id, service_type, customer_name, contact, product_name,
              product_description, commission, sales_amount, json.dumps(service_data), notes))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('サービス書類を作成しました', 'success')
    return redirect(url_for('documents'))

@app.route('/service-document/<int:id>')
@login_required
def service_document_view(id):
    """サービス書類詳細"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM service_documents WHERE id = %s AND user_id = %s", (id, current_user.id))
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM service_documents WHERE id = ? AND user_id = ?", (id, current_user.id))
    
    doc = cur.fetchone()
    cur.close()
    conn.close()
    
    if not doc:
        flash('書類が見つかりません', 'error')
        return redirect(url_for('documents'))
    
    doc = dict(doc)
    if doc.get('service_data'):
        try:
            doc['service_data'] = json.loads(doc['service_data'])
        except:
            doc['service_data'] = {}
    
    return render_template('service_document_view.html', doc=doc)

@app.route('/service-document/<int:id>/pdf')
@login_required
def service_document_pdf(id):
    """サービス書類PDF出力"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM service_documents WHERE id = %s AND user_id = %s", (id, current_user.id))
        row = cur.fetchone()
        doc = dict(row) if row else None
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM service_documents WHERE id = ? AND user_id = ?", (id, current_user.id))
        row = cur.fetchone()
        if row:
            columns = [d[0] for d in cur.description]
            doc = dict(zip(columns, row))
        else:
            doc = None
    
    cur.close()
    conn.close()
    
    if not doc:
        flash('書類が見つかりません', 'error')
        return redirect(url_for('documents'))
    
    if doc.get('service_data'):
        try:
            doc['service_data'] = json.loads(doc['service_data'])
        except:
            doc['service_data'] = {}
    
    return render_template('pdf/service_document_pdf.html', doc=doc)

@app.route('/shikiriosho')
@login_required
def user_shikiriosho_list():
    """受信した精算書一覧（ユーザー用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT s.*, u.display_name as sender_display_name
            FROM shikiriosho s
            LEFT JOIN users u ON s.sender_id = u.id
            WHERE s.recipient_id = %s AND s.status = 'sent'
            ORDER BY s.created_at DESC
        """, (current_user.id,))
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.*, u.display_name as sender_display_name
            FROM shikiriosho s
            LEFT JOIN users u ON s.sender_id = u.id
            WHERE s.recipient_id = ? AND s.status = 'sent'
            ORDER BY s.created_at DESC
        """, (current_user.id,))
    
    shikiriosho_list = [dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    
    return render_template('shikiriosho_list.html', shikiriosho_list=shikiriosho_list)

@app.route('/shikiriosho/view/<int:id>')
@login_required
def user_shikiriosho_view(id):
    """精算書詳細（ユーザー用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT s.*, u.display_name as sender_display_name
            FROM shikiriosho s
            LEFT JOIN users u ON s.sender_id = u.id
            WHERE s.id = %s AND s.recipient_id = %s
        """, (id, current_user.id))
        shikiriosho = cur.fetchone()
        if shikiriosho:
            shikiriosho = dict(shikiriosho)
            # 既読にする
            cur.execute("UPDATE shikiriosho SET is_read = 1 WHERE id = %s", (id,))
        cur.execute("SELECT * FROM shikiriosho_items WHERE shikiriosho_id = %s ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT s.*, u.display_name as sender_display_name
            FROM shikiriosho s
            LEFT JOIN users u ON s.sender_id = u.id
            WHERE s.id = ? AND s.recipient_id = ?
        """, (id, current_user.id))
        shikiriosho = cur.fetchone()
        if shikiriosho:
            shikiriosho = dict(shikiriosho)
            # 既読にする
            cur.execute("UPDATE shikiriosho SET is_read = 1 WHERE id = ?", (id,))
        cur.execute("SELECT * FROM shikiriosho_items WHERE shikiriosho_id = ? ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    
    conn.commit()
    cur.close()
    conn.close()
    
    if not shikiriosho:
        flash('精算書が見つかりません', 'error')
        return redirect(url_for('user_shikiriosho_list'))
    
    return render_template('shikiriosho_view.html', shikiriosho=shikiriosho, items=items)

@app.route('/shikiriosho/download/<int:id>')
@login_required
def user_shikiriosho_download(id):
    """精算書CSVダウンロード（ユーザー用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM shikiriosho WHERE id = %s AND recipient_id = %s", (id, current_user.id))
        shikiriosho = cur.fetchone()
        if shikiriosho:
            shikiriosho = dict(shikiriosho)
        cur.execute("SELECT * FROM shikiriosho_items WHERE shikiriosho_id = %s ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM shikiriosho WHERE id = ? AND recipient_id = ?", (id, current_user.id))
        shikiriosho = cur.fetchone()
        if shikiriosho:
            shikiriosho = dict(shikiriosho)
        cur.execute("SELECT * FROM shikiriosho_items WHERE shikiriosho_id = ? ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    if not shikiriosho:
        flash('精算書が見つかりません', 'error')
        return redirect(url_for('user_shikiriosho_list'))
    
    # CSV作成
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    
    # ヘッダー情報
    writer.writerow(['株式会社開花'])
    writer.writerow([''])
    writer.writerow(['精算書番号', shikiriosho['document_no'], '', '', '発行日', shikiriosho['issue_date']])
    writer.writerow([''])
    writer.writerow(['宛先', shikiriosho['recipient_name'] or ''])
    writer.writerow([''])
    writer.writerow(['下記のとおり、お見積り申し上げます'])
    writer.writerow(['税込合計金額', f"¥{shikiriosho['total_amount']:,}"])
    writer.writerow([''])
    writer.writerow(['No', '品名', '規格', '数量', '単価', '金額'])
    
    for item in items:
        writer.writerow([
            item['item_no'],
            item['product_name'],
            item['specification'] or '',
            item['quantity'],
            f"¥{item['unit_price']:,}",
            f"¥{item['amount']:,}"
        ])
    
    writer.writerow([''])
    writer.writerow(['', '', '', '', '小計', f"¥{shikiriosho['subtotal']:,}"])
    writer.writerow(['', '', '', '', f"消費税（{shikiriosho['tax_rate']}%）", f"¥{shikiriosho['tax_amount']:,}"])
    writer.writerow(['', '', '', '', '合計', f"¥{shikiriosho['total_amount']:,}"])
    
    if shikiriosho['notes']:
        writer.writerow([''])
        writer.writerow(['備考', shikiriosho['notes']])
    
    output.seek(0)
    
    # BOMを追加してExcelで文字化けしないように
    bom = '\ufeff'
    csv_content = bom + output.getvalue()
    
    return send_file(
        io.BytesIO(csv_content.encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f"精算書_{shikiriosho['document_no']}.csv"
    )

# 未読精算書数を取得するヘルパー関数
def get_unread_shikiriosho_count(user_id):
    """未読精算書数を取得"""
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT COUNT(*) as count FROM shikiriosho 
                WHERE recipient_id = %s AND status = 'sent' AND is_read = 0
            """, (user_id,))
            result = cur.fetchone()
            count = result['count'] if result else 0
        else:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) FROM shikiriosho 
                WHERE recipient_id = ? AND status = 'sent' AND is_read = 0
            """, (user_id,))
            result = cur.fetchone()
            count = result[0] if result else 0
        cur.close()
        conn.close()
        return count
    except Exception:
        return 0

# テンプレートに未読精算書数を渡す
@app.context_processor
def inject_unread_shikiriosho():
    try:
        if current_user.is_authenticated:
            return {'unread_shikiriosho_count': get_unread_shikiriosho_count(current_user.id)}
    except Exception:
        pass
    return {'unread_shikiriosho_count': 0}

# ===================
# 買取明細書（ユーザー→管理者）
# ===================

def generate_invoice_no():
    """買取明細書番号を生成"""
    now = datetime.now()
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT COUNT(*) as count FROM invoices WHERE issue_date >= %s", 
                   (now.strftime('%Y-%m-01'),))
        result = cur.fetchone()
        count = (result['count'] if result else 0) + 1
    else:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM invoices WHERE issue_date >= ?", 
                   (now.strftime('%Y-%m-01'),))
        result = cur.fetchone()
        count = (result[0] if result else 0) + 1
    cur.close()
    conn.close()
    return f"INV-{now.strftime('%Y%m')}-{count:04d}"

def get_unread_invoice_count():
    """未読買取明細書数を取得（管理者用）"""
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT COUNT(*) as count FROM invoices 
                WHERE status = 'sent' AND is_read = 0
            """)
            result = cur.fetchone()
            count = result['count'] if result else 0
        else:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) FROM invoices 
                WHERE status = 'sent' AND is_read = 0
            """)
            result = cur.fetchone()
            count = result[0] if result else 0
        cur.close()
        conn.close()
        return count
    except Exception:
        return 0

@app.context_processor
def inject_unread_invoice():
    try:
        if current_user.is_authenticated and current_user.is_admin():
            return {'unread_invoice_count': get_unread_invoice_count()}
    except Exception:
        pass
    return {'unread_invoice_count': 0}

@app.route('/invoices')
@login_required
def user_invoice_list():
    """買取明細書一覧（ユーザー用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT * FROM invoices
            WHERE sender_id = %s
            ORDER BY created_at DESC
        """, (current_user.id,))
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM invoices
            WHERE sender_id = ?
            ORDER BY created_at DESC
        """, (current_user.id,))
    
    invoices = [dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    
    return render_template('invoice_list.html', invoices=invoices)

@app.route('/invoices/add', methods=['GET', 'POST'])
@login_required
def user_invoice_add():
    """買取明細書作成（ユーザー用）"""
    conn = get_db()
    
    if request.method == 'POST':
        issue_date = request.form.get('issue_date')
        payment_due_date = request.form.get('payment_due_date') or None
        recipient_name = request.form.get('recipient_name', '')
        postal_number = request.form.get('postal_number', '')
        bank_info = request.form.get('bank_info', '')
        notes = request.form.get('notes', '')
        status = request.form.get('status', 'draft')
        service_type = request.form.get('service_type', 'normal')
        commission_amount = int(request.form.get('commission_amount', 0) or 0)
        commission_rate = float(request.form.get('commission_rate', 10) or 10)
        
        # 明細データ取得
        tax_categories = request.form.getlist('tax_category[]')
        product_dates = request.form.getlist('product_date[]')
        product_names = request.form.getlist('product_name[]')
        product_codes = request.form.getlist('product_code[]')
        quantities = request.form.getlist('quantity[]')
        units = request.form.getlist('unit[]')
        unit_prices = request.form.getlist('unit_price[]')
        
        # 合計計算
        subtotal = 0
        subtotal_8 = 0
        subtotal_10 = 0
        items = []
        
        for i, name in enumerate(product_names):
            if name.strip():
                qty = int(quantities[i]) if i < len(quantities) and quantities[i] else 1
                price = int(unit_prices[i]) if i < len(unit_prices) and unit_prices[i] else 0
                amount = qty * price
                subtotal += amount
                
                tax_cat = tax_categories[i] if i < len(tax_categories) else '10'
                if tax_cat == '8':
                    subtotal_8 += amount
                else:
                    subtotal_10 += amount
                
                items.append({
                    'item_no': i + 1,
                    'tax_category': tax_cat,
                    'product_date': product_dates[i] if i < len(product_dates) and product_dates[i] else None,
                    'product_name': name,
                    'product_code': product_codes[i] if i < len(product_codes) else '',
                    'quantity': qty,
                    'unit': units[i] if i < len(units) else '',
                    'unit_price': price,
                    'amount': amount
                })
        
        tax_amount_8 = int(subtotal_8 * 0.08)
        tax_amount_10 = int(subtotal_10 * 0.10)
        total_amount = subtotal + tax_amount_8 + tax_amount_10 + commission_amount
        invoice_no = generate_invoice_no()
        
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                INSERT INTO invoices 
                (invoice_no, sender_id, issue_date, payment_due_date, recipient_name, postal_number,
                 subtotal, tax_amount_8, tax_amount_10, total_amount, 
                 service_type, commission_rate, commission_amount, bank_info, notes, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (invoice_no, current_user.id, issue_date, payment_due_date, recipient_name, postal_number,
                  subtotal, tax_amount_8, tax_amount_10, total_amount,
                  service_type, commission_rate, commission_amount, bank_info, notes, status))
            invoice_id = cur.fetchone()['id']
            
            for item in items:
                cur.execute("""
                    INSERT INTO invoice_items 
                    (invoice_id, item_no, tax_category, product_date, product_name, product_code, quantity, unit, unit_price, amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (invoice_id, item['item_no'], item['tax_category'], item['product_date'],
                      item['product_name'], item['product_code'], item['quantity'], item['unit'], item['unit_price'], item['amount']))
        else:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO invoices 
                (invoice_no, sender_id, issue_date, payment_due_date, recipient_name, postal_number,
                 subtotal, tax_amount_8, tax_amount_10, total_amount,
                 service_type, commission_rate, commission_amount, bank_info, notes, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (invoice_no, current_user.id, issue_date, payment_due_date, recipient_name, postal_number,
                  subtotal, tax_amount_8, tax_amount_10, total_amount,
                  service_type, commission_rate, commission_amount, bank_info, notes, status))
            invoice_id = cur.lastrowid
            
            for item in items:
                cur.execute("""
                    INSERT INTO invoice_items 
                    (invoice_id, item_no, tax_category, product_date, product_name, product_code, quantity, unit, unit_price, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (invoice_id, item['item_no'], item['tax_category'], item['product_date'],
                      item['product_name'], item['product_code'], item['quantity'], item['unit'], item['unit_price'], item['amount']))
        
        conn.commit()
        cur.close()
        conn.close()
        
        if status == 'sent':
            flash(f'買取明細書 {invoice_no} を作成・送信しました', 'success')
        else:
            flash(f'買取明細書 {invoice_no} を下書き保存しました', 'success')
        return redirect(url_for('user_invoice_list'))
    
    # GETリクエストの場合はconnを閉じる
    conn.close()
    
    return render_template('invoice_form.html', 
                          invoice_no=generate_invoice_no(),
                          today=datetime.now().strftime('%Y-%m-%d'))

@app.route('/invoices/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def user_invoice_edit(id):
    """買取明細書編集（ユーザー用）"""
    conn = get_db()
    
    # 所有者確認
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM invoices WHERE id = %s AND sender_id = %s", (id, current_user.id))
        invoice = cur.fetchone()
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM invoices WHERE id = ? AND sender_id = ?", (id, current_user.id))
        invoice = cur.fetchone()
    
    if not invoice:
        flash('買取明細書が見つかりません', 'error')
        return redirect(url_for('user_invoice_list'))
    
    invoice = dict(invoice)
    
    # 送信済みは編集不可
    if invoice['status'] == 'sent':
        flash('送信済みの買取明細書は編集できません', 'error')
        return redirect(url_for('user_invoice_list'))
    
    if request.method == 'POST':
        issue_date = request.form.get('issue_date')
        payment_due_date = request.form.get('payment_due_date') or None
        recipient_name = request.form.get('recipient_name', '')
        postal_number = request.form.get('postal_number', '')
        bank_info = request.form.get('bank_info', '')
        notes = request.form.get('notes', '')
        status = request.form.get('status', 'draft')
        
        # 明細データ取得
        tax_categories = request.form.getlist('tax_category[]')
        product_dates = request.form.getlist('product_date[]')
        product_names = request.form.getlist('product_name[]')
        product_codes = request.form.getlist('product_code[]')
        quantities = request.form.getlist('quantity[]')
        units = request.form.getlist('unit[]')
        unit_prices = request.form.getlist('unit_price[]')
        
        # 合計計算
        subtotal = 0
        subtotal_8 = 0
        subtotal_10 = 0
        items = []
        
        for i, name in enumerate(product_names):
            if name.strip():
                qty = int(quantities[i]) if i < len(quantities) and quantities[i] else 1
                price = int(unit_prices[i]) if i < len(unit_prices) and unit_prices[i] else 0
                amount = qty * price
                subtotal += amount
                
                tax_cat = tax_categories[i] if i < len(tax_categories) else '10'
                if tax_cat == '8':
                    subtotal_8 += amount
                else:
                    subtotal_10 += amount
                
                items.append({
                    'item_no': i + 1,
                    'tax_category': tax_cat,
                    'product_date': product_dates[i] if i < len(product_dates) and product_dates[i] else None,
                    'product_name': name,
                    'product_code': product_codes[i] if i < len(product_codes) else '',
                    'quantity': qty,
                    'unit': units[i] if i < len(units) else '',
                    'unit_price': price,
                    'amount': amount
                })
        
        tax_amount_8 = int(subtotal_8 * 0.08)
        tax_amount_10 = int(subtotal_10 * 0.10)
        total_amount = subtotal + tax_amount_8 + tax_amount_10
        
        if DATABASE_URL:
            cur.execute("""
                UPDATE invoices SET
                issue_date = %s, payment_due_date = %s, recipient_name = %s, postal_number = %s,
                subtotal = %s, tax_amount_8 = %s, tax_amount_10 = %s, total_amount = %s,
                bank_info = %s, notes = %s, status = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (issue_date, payment_due_date, recipient_name, postal_number, subtotal, tax_amount_8, tax_amount_10,
                  total_amount, bank_info, notes, status, id))
            
            cur.execute("DELETE FROM invoice_items WHERE invoice_id = %s", (id,))
            
            for item in items:
                cur.execute("""
                    INSERT INTO invoice_items 
                    (invoice_id, item_no, tax_category, product_date, product_name, product_code, quantity, unit, unit_price, amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (id, item['item_no'], item['tax_category'], item['product_date'],
                      item['product_name'], item['product_code'], item['quantity'], item['unit'], item['unit_price'], item['amount']))
        else:
            cur.execute("""
                UPDATE invoices SET
                issue_date = ?, payment_due_date = ?, recipient_name = ?, postal_number = ?,
                subtotal = ?, tax_amount_8 = ?, tax_amount_10 = ?, total_amount = ?,
                bank_info = ?, notes = ?, status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (issue_date, payment_due_date, recipient_name, postal_number, subtotal, tax_amount_8, tax_amount_10,
                  total_amount, bank_info, notes, status, id))
            
            cur.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (id,))
            
            for item in items:
                cur.execute("""
                    INSERT INTO invoice_items 
                    (invoice_id, item_no, tax_category, product_date, product_name, product_code, quantity, unit, unit_price, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (id, item['item_no'], item['tax_category'], item['product_date'],
                      item['product_name'], item['product_code'], item['quantity'], item['unit'], item['unit_price'], item['amount']))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('買取明細書を更新しました', 'success')
        return redirect(url_for('user_invoice_list'))
    
    # 明細データ取得
    if DATABASE_URL:
        cur.execute("SELECT * FROM invoice_items WHERE invoice_id = %s ORDER BY item_no", (id,))
    else:
        cur.execute("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY item_no", (id,))
    
    items = [dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    
    return render_template('invoice_form.html', 
                          invoice=invoice, 
                          items=items,
                          today=datetime.now().strftime('%Y-%m-%d'))

@app.route('/invoices/view/<int:id>')
@login_required
def user_invoice_view(id):
    """買取明細書詳細（ユーザー用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM invoices WHERE id = %s AND sender_id = %s", (id, current_user.id))
        invoice = cur.fetchone()
        if invoice:
            invoice = dict(invoice)
        cur.execute("SELECT * FROM invoice_items WHERE invoice_id = %s ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM invoices WHERE id = ? AND sender_id = ?", (id, current_user.id))
        invoice = cur.fetchone()
        if invoice:
            invoice = dict(invoice)
        cur.execute("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    if not invoice:
        flash('買取明細書が見つかりません', 'error')
        return redirect(url_for('user_invoice_list'))
    
    return render_template('invoice_view.html', invoice=invoice, items=items)

@app.route('/invoices/delete/<int:id>')
@login_required
def user_invoice_delete(id):
    """買取明細書削除（ユーザー用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT status FROM invoices WHERE id = %s AND sender_id = %s", (id, current_user.id))
        invoice = cur.fetchone()
        if invoice and invoice['status'] == 'draft':
            cur.execute("DELETE FROM invoice_items WHERE invoice_id = %s", (id,))
            cur.execute("DELETE FROM invoices WHERE id = %s", (id,))
            conn.commit()
            flash('買取明細書を削除しました', 'success')
        else:
            flash('送信済みの買取明細書は削除できません', 'error')
    else:
        cur = conn.cursor()
        cur.execute("SELECT status FROM invoices WHERE id = ? AND sender_id = ?", (id, current_user.id))
        invoice = cur.fetchone()
        if invoice and invoice['status'] == 'draft':
            cur.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (id,))
            cur.execute("DELETE FROM invoices WHERE id = ?", (id,))
            conn.commit()
            flash('買取明細書を削除しました', 'success')
        else:
            flash('送信済みの買取明細書は削除できません', 'error')
    
    cur.close()
    conn.close()
    return redirect(url_for('user_invoice_list'))

@app.route('/invoices/send/<int:id>')
@login_required
def user_invoice_send(id):
    """買取明細書送信（ユーザー用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("UPDATE invoices SET status = 'sent', updated_at = CURRENT_TIMESTAMP WHERE id = %s AND sender_id = %s", (id, current_user.id))
    else:
        cur = conn.cursor()
        cur.execute("UPDATE invoices SET status = 'sent', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND sender_id = ?", (id, current_user.id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('買取明細書を送信しました', 'success')
    return redirect(url_for('user_invoice_list'))

# ===================
# ユーザー向け見積依頼書
# ===================

@app.route('/mitsumori')
@login_required
def user_mitsumori_list():
    """見積依頼書一覧（ユーザー用）"""
    mitsumori_list = []
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM user_mitsumori WHERE user_id = %s ORDER BY created_at DESC", (current_user.id,))
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM user_mitsumori WHERE user_id = ? ORDER BY created_at DESC", (current_user.id,))
        
        for row in cur.fetchall():
            item = dict(row)
            # datetime を文字列に変換
            for key in ['created_at', 'updated_at', 'issue_date', 'valid_until']:
                if item.get(key) and hasattr(item[key], 'strftime'):
                    item[key] = item[key].strftime('%Y-%m-%d %H:%M:%S') if key.endswith('_at') else item[key].strftime('%Y-%m-%d')
            mitsumori_list.append(item)
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] user_mitsumori_list: {e}", flush=True)
    
    return render_template('mitsumori_list.html', mitsumori_list=mitsumori_list)

@app.route('/mitsumori/add', methods=['GET', 'POST'])
@login_required
def user_mitsumori_add():
    """見積依頼書作成（ユーザー用）"""
    from datetime import datetime
    
    conn = get_db()
    
    if request.method == 'POST':
        issue_date = request.form.get('issue_date')
        valid_until = request.form.get('valid_until') or None
        company_name = request.form.get('company_name', '')
        department = request.form.get('department', '')
        contact_person = request.form.get('contact_person', '')
        address = request.form.get('address', '')
        subject = request.form.get('subject', '')
        notes = request.form.get('notes', '')
        status = request.form.get('status', 'draft')
        
        # 明細項目
        item_names = request.form.getlist('item_name[]')
        quantities = request.form.getlist('quantity[]')
        units = request.form.getlist('unit[]')
        unit_prices = request.form.getlist('unit_price[]')
        
        # 合計金額計算
        total_amount = 0
        items_data = []
        for i, name in enumerate(item_names):
            if name:
                qty = int(quantities[i]) if i < len(quantities) and quantities[i] else 1
                price = int(unit_prices[i]) if i < len(unit_prices) and unit_prices[i] else 0
                amount = qty * price
                total_amount += amount
                items_data.append({
                    'item_no': i + 1,
                    'item_name': name,
                    'quantity': qty,
                    'unit': units[i] if i < len(units) else '',
                    'unit_price': price,
                    'amount': amount
                })
        
        now = datetime.now()
        
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT COUNT(*) as count FROM user_mitsumori WHERE user_id = %s AND issue_date >= %s", 
                       (current_user.id, now.strftime('%Y-%m-01')))
            result = cur.fetchone()
            count = (result['count'] if result else 0) + 1
            document_no = f"UM-{now.strftime('%Y%m')}-{current_user.id}-{count:04d}"
            
            cur.execute("""
                INSERT INTO user_mitsumori (document_no, user_id, issue_date, valid_until, company_name, department, contact_person, address, subject, total_amount, notes, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (document_no, current_user.id, issue_date, valid_until, company_name, department, contact_person, address, subject, total_amount, notes, status))
            mitsumori_id = cur.fetchone()['id']
            
            for item in items_data:
                cur.execute("""
                    INSERT INTO user_mitsumori_items (mitsumori_id, item_no, item_name, quantity, unit, unit_price, amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (mitsumori_id, item['item_no'], item['item_name'], item['quantity'], item['unit'], item['unit_price'], item['amount']))
        else:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as count FROM user_mitsumori WHERE user_id = ? AND issue_date >= ?", 
                       (current_user.id, now.strftime('%Y-%m-01')))
            result = dict(cur.fetchone())
            count = (result['count'] if result else 0) + 1
            document_no = f"UM-{now.strftime('%Y%m')}-{current_user.id}-{count:04d}"
            
            cur.execute("""
                INSERT INTO user_mitsumori (document_no, user_id, issue_date, valid_until, company_name, department, contact_person, address, subject, total_amount, notes, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (document_no, current_user.id, issue_date, valid_until, company_name, department, contact_person, address, subject, total_amount, notes, status))
            mitsumori_id = cur.lastrowid
            
            for item in items_data:
                cur.execute("""
                    INSERT INTO user_mitsumori_items (mitsumori_id, item_no, item_name, quantity, unit, unit_price, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (mitsumori_id, item['item_no'], item['item_name'], item['quantity'], item['unit'], item['unit_price'], item['amount']))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('見積依頼書を作成しました', 'success')
        return redirect(url_for('documents'))
    
    # GETリクエスト
    today = datetime.now().strftime('%Y-%m-%d')
    now = datetime.now()
    my_merchandise = []
    
    try:
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT COUNT(*) as count FROM user_mitsumori WHERE user_id = %s AND issue_date >= %s", 
                       (current_user.id, now.strftime('%Y-%m-01')))
            result = cur.fetchone()
            count = (result['count'] if result else 0) + 1
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as count FROM user_mitsumori WHERE user_id = ? AND issue_date >= ?", 
                       (current_user.id, now.strftime('%Y-%m-01')))
            result = cur.fetchone()
            count = (dict(result)['count'] if result else 0) + 1
        
        document_no = f"UM-{now.strftime('%Y%m')}-{current_user.id}-{count:04d}"
        
        # ユーザーの商品一覧を取得（未売却のみ）
        if DATABASE_URL:
            cur.execute("""
                SELECT id, product_name, brand_name, listing_price, photo_path 
                FROM merchandise 
                WHERE user_id = %s AND sale_date IS NULL 
                ORDER BY id DESC
            """, (current_user.id,))
        else:
            cur.execute("""
                SELECT id, product_name, brand_name, listing_price, photo_path 
                FROM merchandise 
                WHERE user_id = ? AND sale_date IS NULL 
                ORDER BY id DESC
            """, (current_user.id,))
        my_merchandise = [dict(row) for row in cur.fetchall()]
        
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] user_mitsumori_add GET: {e}", flush=True)
        document_no = f"UM-{now.strftime('%Y%m')}-{current_user.id}-0001"
    
    return render_template('mitsumori_form.html', mitsumori=None, items=[], today=today, document_no=document_no, my_merchandise=my_merchandise)

@app.route('/mitsumori/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def user_mitsumori_edit(id):
    """見積依頼書編集（ユーザー用）"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM user_mitsumori WHERE id = %s AND user_id = %s", (id, current_user.id))
        mitsumori = cur.fetchone()
        if mitsumori:
            mitsumori = dict(mitsumori)
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM user_mitsumori WHERE id = ? AND user_id = ?", (id, current_user.id))
        mitsumori = cur.fetchone()
        if mitsumori:
            mitsumori = dict(mitsumori)
    
    if not mitsumori:
        flash('見積依頼書が見つかりません', 'error')
        return redirect(url_for('documents'))
    
    if mitsumori['status'] != 'draft':
        flash('完了済みの見積依頼書は編集できません', 'error')
        return redirect(url_for('documents'))
    
    if request.method == 'POST':
        issue_date = request.form.get('issue_date')
        valid_until = request.form.get('valid_until') or None
        company_name = request.form.get('company_name', '')
        department = request.form.get('department', '')
        contact_person = request.form.get('contact_person', '')
        address = request.form.get('address', '')
        subject = request.form.get('subject', '')
        notes = request.form.get('notes', '')
        status = request.form.get('status', 'draft')
        
        # 明細項目
        item_names = request.form.getlist('item_name[]')
        quantities = request.form.getlist('quantity[]')
        units = request.form.getlist('unit[]')
        unit_prices = request.form.getlist('unit_price[]')
        
        # 合計金額計算
        total_amount = 0
        items_data = []
        for i, name in enumerate(item_names):
            if name:
                qty = int(quantities[i]) if i < len(quantities) and quantities[i] else 1
                price = int(unit_prices[i]) if i < len(unit_prices) and unit_prices[i] else 0
                amount = qty * price
                total_amount += amount
                items_data.append({
                    'item_no': i + 1,
                    'item_name': name,
                    'quantity': qty,
                    'unit': units[i] if i < len(units) else '',
                    'unit_price': price,
                    'amount': amount
                })
        
        if DATABASE_URL:
            cur.execute("""
                UPDATE user_mitsumori SET issue_date = %s, valid_until = %s, company_name = %s, department = %s, 
                contact_person = %s, address = %s, subject = %s, total_amount = %s, notes = %s, status = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (issue_date, valid_until, company_name, department, contact_person, address, subject, total_amount, notes, status, id))
            
            cur.execute("DELETE FROM user_mitsumori_items WHERE mitsumori_id = %s", (id,))
            for item in items_data:
                cur.execute("""
                    INSERT INTO user_mitsumori_items (mitsumori_id, item_no, item_name, quantity, unit, unit_price, amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (id, item['item_no'], item['item_name'], item['quantity'], item['unit'], item['unit_price'], item['amount']))
        else:
            cur.execute("""
                UPDATE user_mitsumori SET issue_date = ?, valid_until = ?, company_name = ?, department = ?, 
                contact_person = ?, address = ?, subject = ?, total_amount = ?, notes = ?, status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (issue_date, valid_until, company_name, department, contact_person, address, subject, total_amount, notes, status, id))
            
            cur.execute("DELETE FROM user_mitsumori_items WHERE mitsumori_id = ?", (id,))
            for item in items_data:
                cur.execute("""
                    INSERT INTO user_mitsumori_items (mitsumori_id, item_no, item_name, quantity, unit, unit_price, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (id, item['item_no'], item['item_name'], item['quantity'], item['unit'], item['unit_price'], item['amount']))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('見積依頼書を更新しました', 'success')
        return redirect(url_for('documents'))
    
    # GETリクエスト - 明細取得
    if DATABASE_URL:
        cur.execute("SELECT * FROM user_mitsumori_items WHERE mitsumori_id = %s ORDER BY item_no", (id,))
    else:
        cur.execute("SELECT * FROM user_mitsumori_items WHERE mitsumori_id = ? ORDER BY item_no", (id,))
    
    items = [dict(row) for row in cur.fetchall()]
    
    # ユーザーの商品一覧を取得（未売却のみ）
    if DATABASE_URL:
        cur.execute("""
            SELECT id, product_name, brand_name, listing_price, photo_path 
            FROM merchandise 
            WHERE user_id = %s AND sale_date IS NULL 
            ORDER BY id DESC
        """, (current_user.id,))
    else:
        cur.execute("""
            SELECT id, product_name, brand_name, listing_price, photo_path 
            FROM merchandise 
            WHERE user_id = ? AND sale_date IS NULL 
            ORDER BY id DESC
        """, (current_user.id,))
    my_merchandise = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return render_template('mitsumori_form.html', mitsumori=mitsumori, items=items, today=None, document_no=mitsumori['document_no'], my_merchandise=my_merchandise)

@app.route('/mitsumori/view/<int:id>')
@login_required
def user_mitsumori_view(id):
    """見積依頼書詳細（ユーザー用）"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM user_mitsumori WHERE id = %s AND user_id = %s", (id, current_user.id))
        mitsumori = cur.fetchone()
        if mitsumori:
            mitsumori = dict(mitsumori)
        cur.execute("SELECT * FROM user_mitsumori_items WHERE mitsumori_id = %s ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM user_mitsumori WHERE id = ? AND user_id = ?", (id, current_user.id))
        mitsumori = cur.fetchone()
        if mitsumori:
            mitsumori = dict(mitsumori)
        cur.execute("SELECT * FROM user_mitsumori_items WHERE mitsumori_id = ? ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    if not mitsumori:
        flash('見積依頼書が見つかりません', 'error')
        return redirect(url_for('documents'))
    
    return render_template('mitsumori_view.html', mitsumori=mitsumori, items=items)

@app.route('/mitsumori/pdf/<int:id>')
@login_required
def user_mitsumori_pdf(id):
    """見積依頼書PDF（ユーザー用）"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM user_mitsumori WHERE id = %s AND user_id = %s", (id, current_user.id))
        mitsumori = cur.fetchone()
        if mitsumori:
            mitsumori = dict(mitsumori)
            cur.execute("SELECT * FROM user_mitsumori_items WHERE mitsumori_id = %s ORDER BY item_no", (id,))
            items = [dict(row) for row in cur.fetchall()]
        else:
            items = []
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM user_mitsumori WHERE id = ? AND user_id = ?", (id, current_user.id))
        row = cur.fetchone()
        if row:
            columns = [d[0] for d in cur.description]
            mitsumori = dict(zip(columns, row))
            cur.execute("SELECT * FROM user_mitsumori_items WHERE mitsumori_id = ? ORDER BY item_no", (id,))
            item_columns = [d[0] for d in cur.description] if cur.description else []
            items = [dict(zip(item_columns, r)) for r in cur.fetchall()]
        else:
            mitsumori = None
            items = []
    
    cur.close()
    conn.close()
    
    if not mitsumori:
        flash('見積依頼書が見つかりません', 'error')
        return redirect(url_for('documents'))
    
    html_content = render_template('pdf/mitsumori_pdf.html', mitsumori=mitsumori, items=items)
    return html_content, 200, {'Content-Type': 'text/html; charset=utf-8'}

@app.route('/mitsumori/delete/<int:id>')
@login_required
def user_mitsumori_delete(id):
    """見積依頼書削除（ユーザー用）"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT status FROM user_mitsumori WHERE id = %s AND user_id = %s", (id, current_user.id))
        mitsumori = cur.fetchone()
        if mitsumori and mitsumori['status'] == 'draft':
            cur.execute("DELETE FROM user_mitsumori_items WHERE mitsumori_id = %s", (id,))
            cur.execute("DELETE FROM user_mitsumori WHERE id = %s", (id,))
            conn.commit()
            flash('見積依頼書を削除しました', 'success')
        else:
            flash('完了済みの見積依頼書は削除できません', 'error')
    else:
        cur = conn.cursor()
        cur.execute("SELECT status FROM user_mitsumori WHERE id = ? AND user_id = ?", (id, current_user.id))
        mitsumori = cur.fetchone()
        if mitsumori and mitsumori['status'] == 'draft':
            cur.execute("DELETE FROM user_mitsumori_items WHERE mitsumori_id = ?", (id,))
            cur.execute("DELETE FROM user_mitsumori WHERE id = ?", (id,))
            conn.commit()
            flash('見積依頼書を削除しました', 'success')
        else:
            flash('完了済みの見積依頼書は削除できません', 'error')
    
    cur.close()
    conn.close()
    return redirect(url_for('documents'))

# ===================
# ユーザー向け計算書
# ===================

@app.route('/keisan')
@login_required
def user_keisan_list():
    """計算書一覧（ユーザー用）"""
    keisan_list = []
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM user_keisan WHERE user_id = %s ORDER BY created_at DESC", (current_user.id,))
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM user_keisan WHERE user_id = ? ORDER BY created_at DESC", (current_user.id,))
        
        for row in cur.fetchall():
            item = dict(row)
            for key in ['created_at', 'updated_at', 'issue_date']:
                if item.get(key) and hasattr(item[key], 'strftime'):
                    item[key] = item[key].strftime('%Y-%m-%d %H:%M:%S') if key.endswith('_at') else item[key].strftime('%Y-%m-%d')
            keisan_list.append(item)
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] user_keisan_list: {e}", flush=True)
    
    return render_template('keisan_list.html', keisan_list=keisan_list)

@app.route('/keisan/add', methods=['GET', 'POST'])
@login_required
def user_keisan_add():
    """計算書作成（ユーザー用）"""
    from datetime import datetime
    
    conn = get_db()
    
    if request.method == 'POST':
        issue_date = request.form.get('issue_date')
        recipient_name = request.form.get('recipient_name', '')
        subject = request.form.get('subject', '')
        notes = request.form.get('notes', '')
        status = request.form.get('status', 'draft')
        
        # 明細項目
        item_names = request.form.getlist('item_name[]')
        quantities = request.form.getlist('quantity[]')
        units = request.form.getlist('unit[]')
        unit_prices = request.form.getlist('unit_price[]')
        
        # 合計金額計算
        total_amount = 0
        items_data = []
        for i, name in enumerate(item_names):
            if name:
                qty = int(quantities[i]) if i < len(quantities) and quantities[i] else 1
                price = int(unit_prices[i]) if i < len(unit_prices) and unit_prices[i] else 0
                amount = qty * price
                total_amount += amount
                items_data.append({
                    'item_no': i + 1,
                    'item_name': name,
                    'quantity': qty,
                    'unit': units[i] if i < len(units) else '',
                    'unit_price': price,
                    'amount': amount
                })
        
        now = datetime.now()
        
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT COUNT(*) as count FROM user_keisan WHERE user_id = %s AND issue_date >= %s", 
                       (current_user.id, now.strftime('%Y-%m-01')))
            result = cur.fetchone()
            count = (result['count'] if result else 0) + 1
            document_no = f"UK-{now.strftime('%Y%m')}-{current_user.id}-{count:04d}"
            
            cur.execute("""
                INSERT INTO user_keisan (document_no, user_id, issue_date, recipient_name, subject, total_amount, notes, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (document_no, current_user.id, issue_date, recipient_name, subject, total_amount, notes, status))
            keisan_id = cur.fetchone()['id']
            
            for item in items_data:
                cur.execute("""
                    INSERT INTO user_keisan_items (keisan_id, item_no, item_name, quantity, unit, unit_price, amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (keisan_id, item['item_no'], item['item_name'], item['quantity'], item['unit'], item['unit_price'], item['amount']))
        else:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) as count FROM user_keisan WHERE user_id = ? AND issue_date >= ?", 
                       (current_user.id, now.strftime('%Y-%m-01')))
            result = dict(cur.fetchone())
            count = (result['count'] if result else 0) + 1
            document_no = f"UK-{now.strftime('%Y%m')}-{current_user.id}-{count:04d}"
            
            cur.execute("""
                INSERT INTO user_keisan (document_no, user_id, issue_date, recipient_name, subject, total_amount, notes, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (document_no, current_user.id, issue_date, recipient_name, subject, total_amount, notes, status))
            keisan_id = cur.lastrowid
            
            for item in items_data:
                cur.execute("""
                    INSERT INTO user_keisan_items (keisan_id, item_no, item_name, quantity, unit, unit_price, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (keisan_id, item['item_no'], item['item_name'], item['quantity'], item['unit'], item['unit_price'], item['amount']))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('計算書を作成しました', 'success')
        return redirect(url_for('documents'))
    
    # GETリクエスト
    today = datetime.now().strftime('%Y-%m-%d')
    now = datetime.now()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT COUNT(*) as count FROM user_keisan WHERE user_id = %s AND issue_date >= %s", 
                   (current_user.id, now.strftime('%Y-%m-01')))
        result = cur.fetchone()
        count = (result['count'] if result else 0) + 1
    else:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as count FROM user_keisan WHERE user_id = ? AND issue_date >= ?", 
                   (current_user.id, now.strftime('%Y-%m-01')))
        result = dict(cur.fetchone())
        count = (result['count'] if result else 0) + 1
    
    document_no = f"UK-{now.strftime('%Y%m')}-{current_user.id}-{count:04d}"
    
    cur.close()
    conn.close()
    
    return render_template('keisan_form.html', keisan=None, items=[], today=today, document_no=document_no)

@app.route('/keisan/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def user_keisan_edit(id):
    """計算書編集（ユーザー用）"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM user_keisan WHERE id = %s AND user_id = %s", (id, current_user.id))
        keisan = cur.fetchone()
        if keisan:
            keisan = dict(keisan)
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM user_keisan WHERE id = ? AND user_id = ?", (id, current_user.id))
        keisan = cur.fetchone()
        if keisan:
            keisan = dict(keisan)
    
    if not keisan:
        flash('計算書が見つかりません', 'error')
        return redirect(url_for('documents'))
    
    # 管理者作成の計算書は編集不可
    if keisan.get('is_admin_created'):
        flash('管理者が発行した計算書は編集できません。閲覧とPDF出力のみ可能です。', 'error')
        return redirect(url_for('user_keisan_view', id=id))
    
    if keisan['status'] != 'draft':
        flash('完了済みの計算書は編集できません', 'error')
        return redirect(url_for('documents'))
    
    if request.method == 'POST':
        issue_date = request.form.get('issue_date')
        recipient_name = request.form.get('recipient_name', '')
        subject = request.form.get('subject', '')
        notes = request.form.get('notes', '')
        status = request.form.get('status', 'draft')
        
        # 明細項目
        item_names = request.form.getlist('item_name[]')
        quantities = request.form.getlist('quantity[]')
        units = request.form.getlist('unit[]')
        unit_prices = request.form.getlist('unit_price[]')
        
        # 合計金額計算
        total_amount = 0
        items_data = []
        for i, name in enumerate(item_names):
            if name:
                qty = int(quantities[i]) if i < len(quantities) and quantities[i] else 1
                price = int(unit_prices[i]) if i < len(unit_prices) and unit_prices[i] else 0
                amount = qty * price
                total_amount += amount
                items_data.append({
                    'item_no': i + 1,
                    'item_name': name,
                    'quantity': qty,
                    'unit': units[i] if i < len(units) else '',
                    'unit_price': price,
                    'amount': amount
                })
        
        if DATABASE_URL:
            cur.execute("""
                UPDATE user_keisan SET issue_date = %s, recipient_name = %s, subject = %s, total_amount = %s, notes = %s, status = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (issue_date, recipient_name, subject, total_amount, notes, status, id))
            
            cur.execute("DELETE FROM user_keisan_items WHERE keisan_id = %s", (id,))
            for item in items_data:
                cur.execute("""
                    INSERT INTO user_keisan_items (keisan_id, item_no, item_name, quantity, unit, unit_price, amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (id, item['item_no'], item['item_name'], item['quantity'], item['unit'], item['unit_price'], item['amount']))
        else:
            cur.execute("""
                UPDATE user_keisan SET issue_date = ?, recipient_name = ?, subject = ?, total_amount = ?, notes = ?, status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (issue_date, recipient_name, subject, total_amount, notes, status, id))
            
            cur.execute("DELETE FROM user_keisan_items WHERE keisan_id = ?", (id,))
            for item in items_data:
                cur.execute("""
                    INSERT INTO user_keisan_items (keisan_id, item_no, item_name, quantity, unit, unit_price, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (id, item['item_no'], item['item_name'], item['quantity'], item['unit'], item['unit_price'], item['amount']))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('計算書を更新しました', 'success')
        return redirect(url_for('documents'))
    
    # GETリクエスト - 明細取得
    if DATABASE_URL:
        cur.execute("SELECT * FROM user_keisan_items WHERE keisan_id = %s ORDER BY item_no", (id,))
    else:
        cur.execute("SELECT * FROM user_keisan_items WHERE keisan_id = ? ORDER BY item_no", (id,))
    
    items = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return render_template('keisan_form.html', keisan=keisan, items=items, today=None, document_no=keisan['document_no'])

@app.route('/keisan/view/<int:id>')
@login_required
def user_keisan_view(id):
    """計算書詳細（ユーザー用）"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM user_keisan WHERE id = %s AND user_id = %s", (id, current_user.id))
        keisan = cur.fetchone()
        if keisan:
            keisan = dict(keisan)
        cur.execute("SELECT * FROM user_keisan_items WHERE keisan_id = %s ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM user_keisan WHERE id = ? AND user_id = ?", (id, current_user.id))
        keisan = cur.fetchone()
        if keisan:
            keisan = dict(keisan)
        cur.execute("SELECT * FROM user_keisan_items WHERE keisan_id = ? ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    if not keisan:
        flash('計算書が見つかりません', 'error')
        return redirect(url_for('documents'))
    
    return render_template('keisan_view.html', keisan=keisan, items=items)

@app.route('/keisan/pdf/<int:id>')
@login_required
def user_keisan_pdf(id):
    """計算書PDF（ユーザー用）"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM user_keisan WHERE id = %s AND user_id = %s", (id, current_user.id))
        keisan = cur.fetchone()
        if keisan:
            keisan = dict(keisan)
            cur.execute("SELECT * FROM user_keisan_items WHERE keisan_id = %s ORDER BY item_no", (id,))
            items = [dict(row) for row in cur.fetchall()]
        else:
            items = []
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM user_keisan WHERE id = ? AND user_id = ?", (id, current_user.id))
        row = cur.fetchone()
        if row:
            columns = [d[0] for d in cur.description]
            keisan = dict(zip(columns, row))
            cur.execute("SELECT * FROM user_keisan_items WHERE keisan_id = ? ORDER BY item_no", (id,))
            item_columns = [d[0] for d in cur.description] if cur.description else []
            items = [dict(zip(item_columns, r)) for r in cur.fetchall()]
        else:
            keisan = None
            items = []
    
    cur.close()
    conn.close()
    
    if not keisan:
        flash('計算書が見つかりません', 'error')
        return redirect(url_for('documents'))
    
    html_content = render_template('pdf/keisan_pdf.html', keisan=keisan, items=items)
    return html_content, 200, {'Content-Type': 'text/html; charset=utf-8'}

@app.route('/keisan/delete/<int:id>')
@login_required
def user_keisan_delete(id):
    """計算書削除（ユーザー用）"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT status, is_admin_created FROM user_keisan WHERE id = %s AND user_id = %s", (id, current_user.id))
        keisan = cur.fetchone()
        if keisan and keisan.get('is_admin_created'):
            flash('管理者が発行した計算書は削除できません', 'error')
        elif keisan and keisan['status'] == 'draft':
            cur.execute("DELETE FROM user_keisan_items WHERE keisan_id = %s", (id,))
            cur.execute("DELETE FROM user_keisan WHERE id = %s", (id,))
            conn.commit()
            flash('計算書を削除しました', 'success')
        else:
            flash('完了済みの計算書は削除できません', 'error')
    else:
        cur = conn.cursor()
        cur.execute("SELECT status, is_admin_created FROM user_keisan WHERE id = ? AND user_id = ?", (id, current_user.id))
        keisan = cur.fetchone()
        if keisan:
            keisan = dict(keisan)
        if keisan and keisan.get('is_admin_created'):
            flash('管理者が発行した計算書は削除できません', 'error')
        elif keisan and keisan['status'] == 'draft':
            cur.execute("DELETE FROM user_keisan_items WHERE keisan_id = ?", (id,))
            cur.execute("DELETE FROM user_keisan WHERE id = ?", (id,))
            conn.commit()
            flash('計算書を削除しました', 'success')
        else:
            flash('完了済みの計算書は削除できません', 'error')
    
    cur.close()
    conn.close()
    return redirect(url_for('documents'))

# ===================
# 買取明細書（管理者用）
# ===================

@app.route('/admin/invoices')
@login_required
@permission_required('invoices')
def admin_invoice_list():
    """買取明細書一覧（管理者用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT i.*, u.display_name as sender_display_name, u.username as sender_username
            FROM invoices i
            LEFT JOIN users u ON i.sender_id = u.id
            WHERE i.status = 'sent'
            ORDER BY i.created_at DESC
        """)
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT i.*, u.display_name as sender_display_name, u.username as sender_username
            FROM invoices i
            LEFT JOIN users u ON i.sender_id = u.id
            WHERE i.status = 'sent'
            ORDER BY i.created_at DESC
        """)
    
    invoices = [dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    
    return render_template('admin/invoice_list.html', invoices=invoices)

@app.route('/admin/invoices/view/<int:id>')
@login_required
@permission_required('invoices')
def admin_invoice_view(id):
    """買取明細書詳細（管理者用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT i.*, u.display_name as sender_display_name, u.username as sender_username
            FROM invoices i
            LEFT JOIN users u ON i.sender_id = u.id
            WHERE i.id = %s
        """, (id,))
        invoice = cur.fetchone()
        if invoice:
            invoice = dict(invoice)
            # 既読にする
            cur.execute("UPDATE invoices SET is_read = 1 WHERE id = %s", (id,))
        cur.execute("SELECT * FROM invoice_items WHERE invoice_id = %s ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT i.*, u.display_name as sender_display_name, u.username as sender_username
            FROM invoices i
            LEFT JOIN users u ON i.sender_id = u.id
            WHERE i.id = ?
        """, (id,))
        invoice = cur.fetchone()
        if invoice:
            invoice = dict(invoice)
            # 既読にする
            cur.execute("UPDATE invoices SET is_read = 1 WHERE id = ?", (id,))
        cur.execute("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    
    conn.commit()
    cur.close()
    conn.close()
    
    if not invoice:
        flash('買取明細書が見つかりません', 'error')
        return redirect(url_for('admin_invoice_list'))
    
    return render_template('admin/invoice_view.html', invoice=invoice, items=items)

@app.route('/admin/invoices/approve/<int:id>')
@login_required
@permission_required('invoices')
def admin_invoice_approve(id):
    """買取明細書承認（管理者用）- 承認時に精算書を自動作成"""
    
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    
    conn = get_db()
    
    try:
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            # 買取明細書情報を取得
            cur.execute("SELECT * FROM invoices WHERE id = %s", (id,))
            invoice = cur.fetchone()
            if not invoice:
                flash('買取明細書が見つかりません', 'error')
                cur.close()
                conn.close()
                return redirect(url_for('admin_invoice_list'))
            
            # 精算書番号を生成（同じ接続内で）
            cur.execute("SELECT COUNT(*) as count FROM shikiriosho WHERE issue_date >= %s", 
                       (now.strftime('%Y-%m-01'),))
            result = cur.fetchone()
            count = (result['count'] if result else 0) + 1
            shikiriosho_no = f"SK-{now.strftime('%Y%m')}-{count:04d}"
            
            # 買取明細書を承認
            cur.execute("""
                UPDATE invoices SET status = 'approved', approved_at = CURRENT_TIMESTAMP, 
                approved_by = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s
            """, (current_user.id, id))
            
            # 税額を安全に取得
            tax_8 = invoice.get('tax_amount_8') or 0
            tax_10 = invoice.get('tax_amount_10') or 0
            subtotal = invoice.get('subtotal') or 0
            total = invoice.get('total_amount') or 0
            
            # 精算書を作成（下書き状態）
            cur.execute("""
                INSERT INTO shikiriosho (document_no, sender_id, recipient_id, recipient_name, 
                    issue_date, subtotal, tax_amount, total_amount, tax_rate, notes, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft')
                RETURNING id
            """, (
                shikiriosho_no,
                current_user.id,
                invoice.get('sender_id'),
                None,
                today,
                subtotal,
                tax_8 + tax_10,
                total,
                10,
                f"買取明細書 {invoice.get('invoice_no', '')} より自動作成"
            ))
            result = cur.fetchone()
            shikiriosho_id = result['id'] if result else None
            
            if not shikiriosho_id:
                raise Exception("精算書の作成に失敗しました")
            
            # 買取明細書明細を取得して精算書明細を作成
            cur.execute("SELECT * FROM invoice_items WHERE invoice_id = %s ORDER BY item_no", (id,))
            items = cur.fetchall()
            
            for item in items:
                cur.execute("""
                    INSERT INTO shikiriosho_items (shikiriosho_id, item_no, product_name, specification, 
                        quantity, unit_price, amount, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    shikiriosho_id,
                    item.get('item_no', 1),
                    item.get('product_name', ''),
                    '',
                    item.get('quantity', 1),
                    item.get('unit_price', 0),
                    item.get('amount', 0),
                    ''
                ))
            
        else:
            cur = conn.cursor()
            
            # 買取明細書情報を取得
            cur.execute("SELECT * FROM invoices WHERE id = ?", (id,))
            invoice_row = cur.fetchone()
            if not invoice_row:
                flash('買取明細書が見つかりません', 'error')
                cur.close()
                conn.close()
                return redirect(url_for('admin_invoice_list'))
            invoice = dict(invoice_row)
            
            # 精算書番号を生成（同じ接続内で）
            cur.execute("SELECT COUNT(*) FROM shikiriosho WHERE issue_date >= ?", 
                       (now.strftime('%Y-%m-01'),))
            result = cur.fetchone()
            count = (result[0] if result else 0) + 1
            shikiriosho_no = f"SK-{now.strftime('%Y%m')}-{count:04d}"
            
            # 買取明細書を承認
            cur.execute("""
                UPDATE invoices SET status = 'approved', approved_at = CURRENT_TIMESTAMP, 
                approved_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
            """, (current_user.id, id))
            
            # 税額を安全に取得
            tax_8 = invoice.get('tax_amount_8') or 0
            tax_10 = invoice.get('tax_amount_10') or 0
            subtotal = invoice.get('subtotal') or 0
            total = invoice.get('total_amount') or 0
            
            # 精算書を作成（下書き状態）
            cur.execute("""
                INSERT INTO shikiriosho (document_no, sender_id, recipient_id, recipient_name, 
                    issue_date, subtotal, tax_amount, total_amount, tax_rate, notes, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')
            """, (
                shikiriosho_no,
                current_user.id,
                invoice.get('sender_id'),
                None,
                today,
                subtotal,
                tax_8 + tax_10,
                total,
                10,
                f"買取明細書 {invoice.get('invoice_no', '')} より自動作成"
            ))
            shikiriosho_id = cur.lastrowid
            
            # 買取明細書明細を取得して精算書明細を作成
            cur.execute("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY item_no", (id,))
            items = cur.fetchall()
            
            for item in items:
                item_dict = dict(item)
                cur.execute("""
                    INSERT INTO shikiriosho_items (shikiriosho_id, item_no, product_name, specification, 
                        quantity, unit_price, amount, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    shikiriosho_id,
                    item_dict.get('item_no', 1),
                    item_dict.get('product_name', ''),
                    '',
                    item_dict.get('quantity', 1),
                    item_dict.get('unit_price', 0),
                    item_dict.get('amount', 0),
                    ''
                ))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash(f'買取明細書を承認し、精算書（{shikiriosho_no}）を下書きとして作成しました。内容を確認して送信してください。', 'success')
        return redirect(url_for('admin_shikiriosho_list'))
        
    except Exception as e:
        print(f"Invoice approve error: {e}")
        import traceback
        traceback.print_exc()
        try:
            conn.rollback()
            conn.close()
        except:
            pass
        flash(f'エラーが発生しました: {str(e)}', 'error')
        return redirect(url_for('admin_invoice_list'))

@app.route('/admin/invoices/reject/<int:id>')
@login_required
@permission_required('invoices')
def admin_invoice_reject(id):
    """買取明細書却下（管理者用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("""
            UPDATE invoices SET status = 'rejected', updated_at = CURRENT_TIMESTAMP WHERE id = %s
        """, (id,))
    else:
        cur = conn.cursor()
        cur.execute("""
            UPDATE invoices SET status = 'rejected', updated_at = CURRENT_TIMESTAMP WHERE id = ?
        """, (id,))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('買取明細書を却下しました', 'warning')
    return redirect(url_for('admin_invoice_list'))

@app.route('/invoices/download/<int:id>')
@login_required
def invoice_download(id):
    """買取明細書CSVダウンロード"""
    conn = get_db()
    
    # 権限確認（送信者または管理者）
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if current_user.is_admin():
            cur.execute("SELECT * FROM invoices WHERE id = %s", (id,))
        else:
            cur.execute("SELECT * FROM invoices WHERE id = %s AND sender_id = %s", (id, current_user.id))
        invoice = cur.fetchone()
        if invoice:
            invoice = dict(invoice)
        cur.execute("SELECT * FROM invoice_items WHERE invoice_id = %s ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        if current_user.is_admin():
            cur.execute("SELECT * FROM invoices WHERE id = ?", (id,))
        else:
            cur.execute("SELECT * FROM invoices WHERE id = ? AND sender_id = ?", (id, current_user.id))
        invoice = cur.fetchone()
        if invoice:
            invoice = dict(invoice)
        cur.execute("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    if not invoice:
        flash('買取明細書が見つかりません', 'error')
        return redirect(url_for('user_invoice_list'))
    
    # CSV作成
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL)
    
    writer.writerow(['買取明細書'])
    writer.writerow([''])
    writer.writerow(['', '', '', '発行日', '', invoice['issue_date']])
    writer.writerow([''])
    writer.writerow(['買取明細書番号', invoice['invoice_no']])
    writer.writerow([''])
    writer.writerow(['', '', '', '支払期限', invoice['payment_due_date'] or ''])
    writer.writerow([''])
    writer.writerow(['請求金額 (税込)', '', '', '', '', '', ''])
    writer.writerow([f"¥{invoice['total_amount']:,}"])
    writer.writerow([''])
    writer.writerow(['「8%」…軽減税率対象、「10」…標準税率'])
    writer.writerow(['税区分', '日付', '品名・品番', '数量', '単位', '単価', '合計'])
    
    for item in items:
        writer.writerow([
            item['tax_category'] + '%' if item['tax_category'] else '10%',
            item['product_date'] or '',
            item['product_name'],
            item['quantity'],
            item['unit'] or '',
            f"¥{item['unit_price']:,}",
            f"¥{item['amount']:,}"
        ])
    
    writer.writerow([''])
    writer.writerow(['備考', invoice['notes'] or ''])
    writer.writerow(['', '', '', '合計', '', '', f"¥{invoice['subtotal']:,}"])
    writer.writerow(['', '', '', '8%対象', '', '10%対象', '消費税'])
    writer.writerow(['', '', '', f"¥{invoice['tax_amount_8']:,}", '', f"¥{invoice['tax_amount_10']:,}", f"¥{invoice['tax_amount_8'] + invoice['tax_amount_10']:,}"])
    writer.writerow(['', '', '', '消費税(8%)', '', '消費税(10%)', '合計'])
    writer.writerow(['', '', '', f"¥{invoice['tax_amount_8']:,}", '', f"¥{invoice['tax_amount_10']:,}", f"¥{invoice['total_amount']:,}"])
    
    if invoice['bank_info']:
        writer.writerow([''])
        writer.writerow(['振込先', invoice['bank_info']])
    
    output.seek(0)
    
    bom = '\ufeff'
    csv_content = bom + output.getvalue()
    
    return send_file(
        io.BytesIO(csv_content.encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f"買取明細書_{invoice['invoice_no']}.csv"
    )

# ===================
# Google Drive連携API
# ===================

@app.route('/api/google-drive/config')
@login_required
def api_google_drive_config():
    """Google Drive API設定を返す"""
    return jsonify({
        'enabled': GOOGLE_DRIVE_ENABLED,
        'apiKey': GOOGLE_API_KEY,
        'clientId': GOOGLE_CLIENT_ID
    })

@app.route('/api/google-drive/download', methods=['POST'])
@login_required
def api_google_drive_download():
    """Google Driveから画像をダウンロードしてサーバーに保存"""
    import requests
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': 'データがありません'}), 400
    
    file_id = data.get('fileId')
    file_name = data.get('fileName', 'image.jpg')
    access_token = data.get('accessToken')
    
    if not file_id or not access_token:
        return jsonify({'success': False, 'error': 'ファイルIDまたはアクセストークンがありません'}), 400
    
    try:
        # Google Drive APIから画像をダウンロード
        download_url = f'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media'
        headers = {'Authorization': f'Bearer {access_token}'}
        
        response = requests.get(download_url, headers=headers, timeout=30)
        
        if response.status_code != 200:
            return jsonify({'success': False, 'error': f'ダウンロードエラー: {response.status_code}'}), 400
        
        # ファイル名を安全な形式に変換
        safe_filename = secure_filename(file_name)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        final_filename = f'{timestamp}_{safe_filename}'
        
        # uploadsフォルダに保存
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], final_filename)
        with open(save_path, 'wb') as f:
            f.write(response.content)
        
        return jsonify({
            'success': True,
            'filePath': f'uploads/{final_filename}',
            'fileName': final_filename
        })
        
    except requests.exceptions.Timeout:
        return jsonify({'success': False, 'error': 'ダウンロードがタイムアウトしました'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ===================
# 商品選択API
# ===================

@app.route('/api/products')
@login_required
def api_get_products():
    """ユーザーの商品一覧をJSON形式で取得
    パラメータ:
    - sold_only=1: 売却済み商品のみ
    - inventory_only=1: 在庫（未売却）商品のみ
    """
    sold_only = request.args.get('sold_only')
    inventory_only = request.args.get('inventory_only')
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # フィルター条件を構築
        where_clause = "WHERE user_id = %s"
        params = [current_user.id]
        
        if sold_only == '1':
            where_clause += " AND sale_date IS NOT NULL"
        elif inventory_only == '1':
            where_clause += " AND sale_date IS NULL"
        
        cur.execute(f"""
            SELECT id, product_name, brand_name, purchase_price, listing_price, sale_price, sale_date, is_shipped
            FROM merchandise 
            {where_clause}
            ORDER BY created_at DESC
        """, params)
    else:
        cur = conn.cursor()
        
        # フィルター条件を構築
        where_clause = "WHERE user_id = ?"
        params = [current_user.id]
        
        if sold_only == '1':
            where_clause += " AND sale_date IS NOT NULL"
        elif inventory_only == '1':
            where_clause += " AND sale_date IS NULL"
        
        cur.execute(f"""
            SELECT id, product_name, brand_name, purchase_price, listing_price, sale_price, sale_date, is_shipped
            FROM merchandise 
            {where_clause}
            ORDER BY created_at DESC
        """, params)
    
    products = [dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    
    return jsonify(products)

@app.route('/api/admin/products')
@login_required
@admin_required
def api_get_all_products():
    """全商品一覧をJSON形式で取得（管理者用）
    パラメータ:
    - user_id: ユーザーIDで絞り込み
    - sold_only=1: 売却済み商品のみ
    - inventory_only=1: 在庫（未売却）商品のみ
    """
    user_id = request.args.get('user_id')
    sold_only = request.args.get('sold_only')
    inventory_only = request.args.get('inventory_only')
    
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # フィルター条件を構築
        where_conditions = []
        params = []
        
        if user_id:
            where_conditions.append("m.user_id = %s")
            params.append(user_id)
        
        if sold_only == '1':
            where_conditions.append("m.sale_date IS NOT NULL")
        elif inventory_only == '1':
            where_conditions.append("m.sale_date IS NULL")
        
        where_clause = "WHERE " + " AND ".join(where_conditions) if where_conditions else ""
        
        if params:
            cur.execute(f"""
                SELECT m.id, m.product_name, m.brand_name, m.purchase_price, m.listing_price, m.sale_price, m.sale_date, m.is_shipped,
                       u.display_name as user_name
                FROM merchandise m
                LEFT JOIN users u ON m.user_id = u.id
                {where_clause}
                ORDER BY m.created_at DESC
            """, params)
        else:
            cur.execute(f"""
                SELECT m.id, m.product_name, m.brand_name, m.purchase_price, m.listing_price, m.sale_price, m.sale_date, m.is_shipped,
                       u.display_name as user_name
                FROM merchandise m
                LEFT JOIN users u ON m.user_id = u.id
                {where_clause}
                ORDER BY m.created_at DESC
            """)
    else:
        cur = conn.cursor()
        
        # フィルター条件を構築
        where_conditions = []
        params = []
        
        if user_id:
            where_conditions.append("m.user_id = ?")
            params.append(user_id)
        
        if sold_only == '1':
            where_conditions.append("m.sale_date IS NOT NULL")
        elif inventory_only == '1':
            where_conditions.append("m.sale_date IS NULL")
        
        where_clause = "WHERE " + " AND ".join(where_conditions) if where_conditions else ""
        
        if params:
            cur.execute(f"""
                SELECT m.id, m.product_name, m.brand_name, m.purchase_price, m.listing_price, m.sale_price, m.sale_date, m.is_shipped,
                       u.display_name as user_name
                FROM merchandise m
                LEFT JOIN users u ON m.user_id = u.id
                {where_clause}
                ORDER BY m.created_at DESC
            """, params)
        else:
            cur.execute(f"""
                SELECT m.id, m.product_name, m.brand_name, m.purchase_price, m.listing_price, m.sale_price, m.sale_date, m.is_shipped,
                       u.display_name as user_name
                FROM merchandise m
                LEFT JOIN users u ON m.user_id = u.id
                {where_clause}
                ORDER BY m.created_at DESC
            """)
    
    products = [dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    
    return jsonify(products)

# ===================
# PDF出力
# ===================

@app.route('/shikiriosho/pdf/<int:id>')
@login_required
def shikiriosho_pdf(id):
    """精算書PDF出力"""
    conn = get_db()
    
    # 権限確認（送信者または受信者または管理者）
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if current_user.is_admin():
            cur.execute("SELECT * FROM shikiriosho WHERE id = %s", (id,))
        else:
            cur.execute("SELECT * FROM shikiriosho WHERE id = %s AND (sender_id = %s OR recipient_id = %s)", 
                       (id, current_user.id, current_user.id))
        shikiriosho = cur.fetchone()
        if shikiriosho:
            shikiriosho = dict(shikiriosho)
        cur.execute("SELECT * FROM shikiriosho_items WHERE shikiriosho_id = %s ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        if current_user.is_admin():
            cur.execute("SELECT * FROM shikiriosho WHERE id = ?", (id,))
        else:
            cur.execute("SELECT * FROM shikiriosho WHERE id = ? AND (sender_id = ? OR recipient_id = ?)", 
                       (id, current_user.id, current_user.id))
        row = cur.fetchone()
        if row:
            columns = [d[0] for d in cur.description]
            shikiriosho = dict(zip(columns, row))
        else:
            shikiriosho = None
        cur.execute("SELECT * FROM shikiriosho_items WHERE shikiriosho_id = ? ORDER BY item_no", (id,))
        item_columns = [d[0] for d in cur.description] if cur.description else []
        items = [dict(zip(item_columns, r)) for r in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    if not shikiriosho:
        flash('精算書が見つかりません', 'error')
        return redirect(url_for('index'))
    
    # PDF用HTMLをレンダリング
    html_content = render_template('pdf/shikiriosho_pdf.html', shikiriosho=shikiriosho, items=items)
    
    return html_content, 200, {'Content-Type': 'text/html; charset=utf-8'}

@app.route('/invoices/pdf/<int:id>')
@login_required
def invoice_pdf(id):
    """買取明細書PDF出力"""
    conn = get_db()
    
    # 権限確認（送信者または管理者）
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if current_user.is_admin():
            cur.execute("SELECT * FROM invoices WHERE id = %s", (id,))
        else:
            cur.execute("SELECT * FROM invoices WHERE id = %s AND sender_id = %s", (id, current_user.id))
        invoice = cur.fetchone()
        if invoice:
            invoice = dict(invoice)
        cur.execute("SELECT * FROM invoice_items WHERE invoice_id = %s ORDER BY item_no", (id,))
        items = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        if current_user.is_admin():
            cur.execute("SELECT * FROM invoices WHERE id = ?", (id,))
        else:
            cur.execute("SELECT * FROM invoices WHERE id = ? AND sender_id = ?", (id, current_user.id))
        row = cur.fetchone()
        if row:
            columns = [d[0] for d in cur.description]
            invoice = dict(zip(columns, row))
        else:
            invoice = None
        cur.execute("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY item_no", (id,))
        item_columns = [d[0] for d in cur.description] if cur.description else []
        items = [dict(zip(item_columns, r)) for r in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    if not invoice:
        flash('買取明細書が見つかりません', 'error')
        return redirect(url_for('index'))
    
    # PDF用HTMLをレンダリング
    html_content = render_template('pdf/invoice_pdf.html', invoice=invoice, items=items)
    
    return html_content, 200, {'Content-Type': 'text/html; charset=utf-8'}

# ===================
# 書類管理ダッシュボード
# ===================
@app.route('/admin/documents')
@login_required
@admin_required
def admin_documents_dashboard():
    """書類管理ダッシュボード"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 精算書カウント（shikirioshoテーブルを使用）
        cur.execute("SELECT COUNT(*) as count FROM shikiriosho")
        seisan_count = cur.fetchone()['count']
        
        # 見積依頼書カウント（ユーザーからの送信済み）
        cur.execute("SELECT COUNT(*) as count FROM user_mitsumori WHERE status = 'sent'")
        mitsumori_new_count = cur.fetchone()['count']
        
        cur.execute("SELECT COUNT(*) as count FROM user_mitsumori")
        mitsumori_count = cur.fetchone()['count']
        
        # 買取明細書カウント
        cur.execute("SELECT COUNT(*) as count FROM invoices")
        kaitori_count = cur.fetchone()['count']
        
        # 承認待ち買取明細書
        cur.execute("""
            SELECT i.*, u.display_name as sender_name 
            FROM invoices i 
            LEFT JOIN users u ON i.sender_id = u.id 
            WHERE i.status = 'sent' 
            ORDER BY i.created_at DESC LIMIT 10
        """)
        pending_invoices = [dict(row) for row in cur.fetchall()]
        
        # ユーザーからの見積依頼書（ユーザーごとにまとめる）
        cur.execute("""
            SELECT u.id as user_id, u.display_name, u.username,
                   COUNT(m.id) as mitsumori_count,
                   SUM(CASE WHEN m.status = 'completed' THEN 1 ELSE 0 END) as completed_count,
                   SUM(CASE WHEN m.status = 'draft' THEN 1 ELSE 0 END) as draft_count
            FROM users u
            INNER JOIN user_mitsumori m ON u.id = m.user_id
            WHERE u.role != 'admin' AND u.role != 'owner'
            GROUP BY u.id, u.display_name, u.username
            ORDER BY MAX(m.created_at) DESC
        """)
        user_mitsumori_summary = [dict(row) for row in cur.fetchall()]
        
        # 各ユーザーの見積依頼書詳細を取得
        user_mitsumori_details = {}
        for user in user_mitsumori_summary:
            cur.execute("""
                SELECT * FROM user_mitsumori 
                WHERE user_id = %s 
                ORDER BY created_at DESC
            """, (user['user_id'],))
            user_mitsumori_details[user['user_id']] = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        
        # 精算書カウント
        cur.execute("SELECT COUNT(*) as count FROM shikiriosho")
        result = cur.fetchone()
        seisan_count = result[0] if result else 0
        
        # 見積依頼書カウント（ユーザーからの送信済み）
        cur.execute("SELECT COUNT(*) as count FROM user_mitsumori WHERE status = 'sent'")
        result = cur.fetchone()
        mitsumori_new_count = result[0] if result else 0
        
        cur.execute("SELECT COUNT(*) as count FROM user_mitsumori")
        result = cur.fetchone()
        mitsumori_count = result[0] if result else 0
        
        # 買取明細書カウント
        cur.execute("SELECT COUNT(*) as count FROM invoices")
        result = cur.fetchone()
        kaitori_count = result[0] if result else 0
        
        # 承認待ち買取明細書
        cur.execute("""
            SELECT i.*, u.display_name as sender_name 
            FROM invoices i 
            LEFT JOIN users u ON i.sender_id = u.id 
            WHERE i.status = 'sent' 
            ORDER BY i.created_at DESC LIMIT 10
        """)
        columns = [d[0] for d in cur.description]
        pending_invoices = [dict(zip(columns, row)) for row in cur.fetchall()]
        
        # ユーザーからの見積依頼書（ユーザーごとにまとめる）
        cur.execute("""
            SELECT u.id as user_id, u.display_name, u.username,
                   COUNT(m.id) as mitsumori_count,
                   SUM(CASE WHEN m.status = 'completed' THEN 1 ELSE 0 END) as completed_count,
                   SUM(CASE WHEN m.status = 'draft' THEN 1 ELSE 0 END) as draft_count
            FROM users u
            INNER JOIN user_mitsumori m ON u.id = m.user_id
            WHERE u.role != 'admin' AND u.role != 'owner'
            GROUP BY u.id, u.display_name, u.username
            ORDER BY MAX(m.created_at) DESC
        """)
        columns = [d[0] for d in cur.description]
        user_mitsumori_summary = [dict(zip(columns, row)) for row in cur.fetchall()]
        
        # 各ユーザーの見積依頼書詳細を取得
        user_mitsumori_details = {}
        for user in user_mitsumori_summary:
            cur.execute("""
                SELECT * FROM user_mitsumori 
                WHERE user_id = ? 
                ORDER BY created_at DESC
            """, (user['user_id'],))
            columns = [d[0] for d in cur.description]
            user_mitsumori_details[user['user_id']] = [dict(zip(columns, row)) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return render_template('admin/documents_dashboard.html',
        seisan_count=seisan_count,
        mitsumori_count=mitsumori_count,
        mitsumori_new_count=mitsumori_new_count,
        kaitori_count=kaitori_count,
        pending_invoices=pending_invoices,
        user_mitsumori_summary=user_mitsumori_summary,
        user_mitsumori_details=user_mitsumori_details
    )

# ===================
# ユーザー見積依頼書詳細（管理者用）
# ===================
@app.route('/admin/user-mitsumori/<int:id>')
@login_required
@admin_required
def admin_user_mitsumori_view(id):
    """ユーザーの見積依頼書詳細表示（管理者用）"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT m.*, u.display_name as user_name, u.username
            FROM user_mitsumori m
            LEFT JOIN users u ON m.user_id = u.id
            WHERE m.id = %s
        """, (id,))
        mitsumori = cur.fetchone()
        
        if mitsumori:
            mitsumori = dict(mitsumori)
            cur.execute("SELECT * FROM user_mitsumori_items WHERE mitsumori_id = %s ORDER BY item_no", (id,))
            items = [dict(row) for row in cur.fetchall()]
        else:
            items = []
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT m.*, u.display_name as user_name, u.username
            FROM user_mitsumori m
            LEFT JOIN users u ON m.user_id = u.id
            WHERE m.id = ?
        """, (id,))
        row = cur.fetchone()
        if row:
            columns = [d[0] for d in cur.description]
            mitsumori = dict(zip(columns, row))
            cur.execute("SELECT * FROM user_mitsumori_items WHERE mitsumori_id = ? ORDER BY item_no", (id,))
            columns = [d[0] for d in cur.description]
            items = [dict(zip(columns, row)) for row in cur.fetchall()]
        else:
            mitsumori = None
            items = []
    
    cur.close()
    conn.close()
    
    if not mitsumori:
        flash('見積依頼書が見つかりません', 'error')
        return redirect(url_for('admin_documents_dashboard'))
    
    return render_template('admin/user_mitsumori_view.html', mitsumori=mitsumori, items=items)

# ===================
# 買取明細書（管理者用）
# ===================
@app.route('/admin/kaitori')
@login_required
@admin_required
def admin_kaitori_list():
    """買取明細書一覧"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT i.*, u.display_name as user_display_name
            FROM invoices i
            LEFT JOIN users u ON i.sender_id = u.id
            WHERE i.invoice_no LIKE 'KT-%'
            ORDER BY i.created_at DESC
        """)
        kaitori_list = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT i.*, u.display_name as user_display_name
            FROM invoices i
            LEFT JOIN users u ON i.sender_id = u.id
            WHERE i.invoice_no LIKE 'KT-%'
            ORDER BY i.created_at DESC
        """)
        columns = [d[0] for d in cur.description]
        kaitori_list = [dict(zip(columns, row)) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return render_template('admin/kaitori_list.html', kaitori_list=kaitori_list)

@app.route('/admin/kaitori/add', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_kaitori_add():
    """買取明細書作成"""
    from datetime import datetime
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()
    
    # ユーザー一覧を取得
    cur.execute("SELECT id, username, display_name FROM users ORDER BY display_name")
    if DATABASE_URL:
        users = [dict(row) for row in cur.fetchall()]
    else:
        columns = [d[0] for d in cur.description]
        users = [dict(zip(columns, row)) for row in cur.fetchall()]
    
    if request.method == 'POST':
        try:
            # フォームデータを取得
            issue_date = request.form.get('issue_date')
            seller_id = request.form.get('seller_id') or None
            notes = request.form.get('notes', '')
            status = request.form.get('status', 'draft')
            
            # 商品データを取得
            item_names = request.form.getlist('item_name[]')
            brand_names = request.form.getlist('brand_name[]')
            purchase_dates = request.form.getlist('purchase_date[]')
            item_conditions = request.form.getlist('item_condition[]')
            amounts = request.form.getlist('amount[]')
            merchandise_ids = request.form.getlist('merchandise_id[]')
            
            # 合計金額を計算
            total_amount = 0
            for amount in amounts:
                if amount:
                    total_amount += int(amount)
            
            # 書類番号を生成
            if DATABASE_URL:
                cur.execute("SELECT COUNT(*) as cnt FROM invoices WHERE invoice_no LIKE %s", (f"KT-{datetime.now().strftime('%Y%m%d')}%",))
            else:
                cur.execute("SELECT COUNT(*) as cnt FROM invoices WHERE invoice_no LIKE ?", (f"KT-{datetime.now().strftime('%Y%m%d')}%",))
            
            if DATABASE_URL:
                count_result = cur.fetchone()
                count = count_result['cnt'] if count_result else 0
            else:
                count = cur.fetchone()[0]
            
            document_no = f"KT-{datetime.now().strftime('%Y%m%d')}-{count + 1:03d}"
            
            # invoicesテーブルに保存
            if DATABASE_URL:
                cur.execute("""
                    INSERT INTO invoices (invoice_no, issue_date, sender_id, total_amount, 
                        status, notes, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (document_no, issue_date, seller_id, total_amount, status, notes, datetime.now()))
                invoice_id = cur.fetchone()['id']
            else:
                cur.execute("""
                    INSERT INTO invoices (invoice_no, issue_date, sender_id, total_amount, 
                        status, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (document_no, issue_date, seller_id, total_amount, status, notes, datetime.now()))
                invoice_id = cur.lastrowid
            
            # 商品明細を保存
            for i, item_name in enumerate(item_names):
                if item_name.strip():
                    amount = int(amounts[i]) if i < len(amounts) and amounts[i] else 0
                    brand = brand_names[i] if i < len(brand_names) else ''
                    p_date = purchase_dates[i] if i < len(purchase_dates) else None
                    condition = item_conditions[i] if i < len(item_conditions) else ''
                    merch_id = merchandise_ids[i] if i < len(merchandise_ids) and merchandise_ids[i] else None
                    
                    if DATABASE_URL:
                        cur.execute("""
                            INSERT INTO invoice_items (invoice_id, item_no, product_name, quantity, amount)
                            VALUES (%s, %s, %s, %s, %s)
                        """, (invoice_id, i + 1, item_name, 1, amount))
                    else:
                        cur.execute("""
                            INSERT INTO invoice_items (invoice_id, item_no, product_name, quantity, amount)
                            VALUES (?, ?, ?, ?, ?)
                        """, (invoice_id, i + 1, item_name, 1, amount))
            
            conn.commit()
            flash('買取明細書を保存しました', 'success')
            cur.close()
            conn.close()
            return redirect(url_for('admin_kaitori_list'))
            
        except Exception as e:
            conn.rollback()
            flash(f'保存に失敗しました: {str(e)}', 'error')
            cur.close()
            conn.close()
    
    cur.close()
    conn.close()
    
    today = datetime.now().strftime('%Y-%m-%d')
    document_no = f"KT-{datetime.now().strftime('%Y%m%d')}-001"
    
    return render_template('admin/kaitori_form.html', 
        kaitori=None, 
        users=users, 
        today=today,
        document_no=document_no,
        items=None
    )

@app.route('/admin/kaitori/<int:id>')
@login_required
@admin_required
def admin_kaitori_view(id):
    """買取明細書詳細"""
    flash('この機能は準備中です', 'info')
    return redirect(url_for('admin_kaitori_list'))

@app.route('/admin/kaitori/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_kaitori_edit(id):
    """買取明細書編集"""
    flash('この機能は準備中です', 'info')
    return redirect(url_for('admin_kaitori_list'))

@app.route('/admin/kaitori/<int:id>/delete')
@login_required
@admin_required
def admin_kaitori_delete(id):
    """買取明細書削除"""
    flash('この機能は準備中です', 'info')
    return redirect(url_for('admin_kaitori_list'))

@app.route('/admin/kaitori/<int:id>/pdf')
@login_required
@admin_required
def admin_kaitori_pdf(id):
    """買取明細書PDF"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM invoices WHERE id = %s", (id,))
        invoice = cur.fetchone()
        if invoice:
            invoice = dict(invoice)
            cur.execute("SELECT * FROM invoice_items WHERE invoice_id = %s ORDER BY id", (id,))
            items = [dict(row) for row in cur.fetchall()]
        else:
            items = []
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM invoices WHERE id = ?", (id,))
        row = cur.fetchone()
        if row:
            columns = [d[0] for d in cur.description]
            invoice = dict(zip(columns, row))
            cur.execute("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY id", (id,))
            item_columns = [d[0] for d in cur.description] if cur.description else []
            items = [dict(zip(item_columns, r)) for r in cur.fetchall()]
        else:
            invoice = None
            items = []
    
    cur.close()
    conn.close()
    
    if not invoice:
        flash('買取明細書が見つかりません', 'error')
        return redirect(url_for('admin_kaitori_list'))
    
    html_content = render_template('pdf/invoice_pdf.html', invoice=invoice, items=items)
    return html_content, 200, {'Content-Type': 'text/html; charset=utf-8'}

# ===================
# 精算書（管理者用）
# ===================
@app.route('/admin/seisan')
@login_required
@admin_required
def admin_seisan_list():
    """精算書一覧"""
    return render_template('admin/seisan_list.html', seisan_list=[])

@app.route('/admin/seisan/add', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_seisan_add():
    """精算書作成"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, username, display_name FROM users ORDER BY display_name")
        users = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("SELECT id, username, display_name FROM users ORDER BY display_name")
        users = [dict(row) for row in cur.fetchall()]
    cur.close()
    conn.close()
    
    from datetime import datetime
    today = datetime.now().strftime('%Y-%m-%d')
    document_no = f"SS-{datetime.now().strftime('%Y%m%d')}-001"
    
    return render_template('admin/seisan_form.html', 
        seisan=None, 
        users=users, 
        today=today,
        document_no=document_no,
        items=None
    )

@app.route('/admin/seisan/<int:id>')
@login_required
@admin_required
def admin_seisan_view(id):
    """精算書詳細"""
    flash('この機能は準備中です', 'info')
    return redirect(url_for('admin_seisan_list'))

@app.route('/admin/seisan/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_seisan_edit(id):
    """精算書編集"""
    flash('この機能は準備中です', 'info')
    return redirect(url_for('admin_seisan_list'))

@app.route('/admin/seisan/<int:id>/delete')
@login_required
@admin_required
def admin_seisan_delete(id):
    """精算書削除"""
    flash('この機能は準備中です', 'info')
    return redirect(url_for('admin_seisan_list'))

@app.route('/admin/seisan/<int:id>/send')
@login_required
@admin_required
def admin_seisan_send(id):
    """精算書送信"""
    flash('この機能は準備中です', 'info')
    return redirect(url_for('admin_seisan_list'))

@app.route('/admin/seisan/<int:id>/pdf')
@login_required
@admin_required
def admin_seisan_pdf(id):
    """精算書PDF"""
    flash('この機能は準備中です', 'info')
    return redirect(url_for('admin_seisan_list'))

# ===================
# 見積依頼書（管理者用）
# ===================
@app.route('/admin/mitsumori')
@login_required
@admin_required
def admin_mitsumori_list():
    """見積依頼書一覧（ユーザーが作成した見積依頼書を表示）"""
    mitsumori_list = []
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute('''
                SELECT m.*, u.display_name as user_name, u.username
                FROM user_mitsumori m
                LEFT JOIN users u ON m.user_id = u.id
                ORDER BY m.created_at DESC
            ''')
            mitsumori_list = [dict(row) for row in cur.fetchall()]
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute('''
                SELECT m.*, u.display_name as user_name, u.username
                FROM user_mitsumori m
                LEFT JOIN users u ON m.user_id = u.id
                ORDER BY m.created_at DESC
            ''')
            mitsumori_list = [dict(row) for row in cur.fetchall()]
        
        # datetime を文字列に変換
        for m in mitsumori_list:
            if m.get('issue_date') and hasattr(m['issue_date'], 'strftime'):
                m['issue_date'] = m['issue_date'].strftime('%Y-%m-%d')
            if m.get('valid_until') and hasattr(m['valid_until'], 'strftime'):
                m['valid_until'] = m['valid_until'].strftime('%Y-%m-%d')
            if m.get('created_at') and hasattr(m['created_at'], 'strftime'):
                m['created_at'] = m['created_at'].strftime('%Y-%m-%d %H:%M')
        
        cur.close()
        conn.close()
    except Exception as e:
        import traceback
        print(f"[ERROR] admin_mitsumori_list: {e}", flush=True)
        traceback.print_exc()
    
    return render_template('admin/mitsumori_list.html', mitsumori_list=mitsumori_list)

@app.route('/admin/mitsumori/add', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_mitsumori_add():
    """見積依頼書作成"""
    from datetime import datetime
    today = datetime.now().strftime('%Y-%m-%d')
    document_no = f"MT-{datetime.now().strftime('%Y%m%d')}-001"
    
    return render_template('admin/mitsumori_form.html', 
        mitsumori=None, 
        today=today,
        document_no=document_no,
        items=None
    )

@app.route('/admin/mitsumori/<int:id>')
@login_required
@admin_required
def admin_mitsumori_view(id):
    """見積依頼書詳細"""
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute('''
                SELECT m.*, u.display_name as user_name, u.username
                FROM user_mitsumori m
                LEFT JOIN users u ON m.user_id = u.id
                WHERE m.id = %s
            ''', (id,))
            mitsumori = cur.fetchone()
            if mitsumori:
                mitsumori = dict(mitsumori)
                cur.execute('SELECT * FROM user_mitsumori_items WHERE mitsumori_id = %s ORDER BY item_no', (id,))
                items = [dict(row) for row in cur.fetchall()]
            else:
                items = []
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute('''
                SELECT m.*, u.display_name as user_name, u.username
                FROM user_mitsumori m
                LEFT JOIN users u ON m.user_id = u.id
                WHERE m.id = ?
            ''', (id,))
            mitsumori = cur.fetchone()
            if mitsumori:
                mitsumori = dict(mitsumori)
                cur.execute('SELECT * FROM user_mitsumori_items WHERE mitsumori_id = ? ORDER BY item_no', (id,))
                items = [dict(row) for row in cur.fetchall()]
            else:
                items = []
        
        cur.close()
        conn.close()
        
        if not mitsumori:
            flash('見積依頼書が見つかりません', 'error')
            return redirect(url_for('admin_mitsumori_list'))
        
        # datetime を文字列に変換
        if mitsumori.get('issue_date') and hasattr(mitsumori['issue_date'], 'strftime'):
            mitsumori['issue_date'] = mitsumori['issue_date'].strftime('%Y-%m-%d')
        if mitsumori.get('valid_until') and hasattr(mitsumori['valid_until'], 'strftime'):
            mitsumori['valid_until'] = mitsumori['valid_until'].strftime('%Y-%m-%d')
        
        return render_template('admin/mitsumori_view.html', mitsumori=mitsumori, items=items)
    except Exception as e:
        import traceback
        print(f"[ERROR] admin_mitsumori_view: {e}", flush=True)
        traceback.print_exc()
        flash(f'エラーが発生しました: {str(e)}', 'error')
        return redirect(url_for('admin_mitsumori_list'))

@app.route('/admin/mitsumori/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_mitsumori_edit(id):
    """見積依頼書編集"""
    flash('この機能は準備中です', 'info')
    return redirect(url_for('admin_mitsumori_list'))

@app.route('/admin/mitsumori/<int:id>/delete')
@login_required
@admin_required
def admin_mitsumori_delete(id):
    """見積依頼書削除"""
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute('DELETE FROM user_mitsumori_items WHERE mitsumori_id = %s', (id,))
            cur.execute('DELETE FROM user_mitsumori WHERE id = %s', (id,))
        else:
            cur = conn.cursor()
            cur.execute('DELETE FROM user_mitsumori_items WHERE mitsumori_id = ?', (id,))
            cur.execute('DELETE FROM user_mitsumori WHERE id = ?', (id,))
        conn.commit()
        cur.close()
        conn.close()
        flash('見積依頼書を削除しました', 'success')
    except Exception as e:
        flash(f'削除エラー: {str(e)}', 'error')
    return redirect(url_for('admin_mitsumori_list'))

@app.route('/admin/mitsumori/<int:id>/approve', methods=['POST'])
@login_required
@admin_required
def admin_mitsumori_approve(id):
    """見積依頼書承認"""
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute("UPDATE user_mitsumori SET status = 'approved' WHERE id = %s", (id,))
        else:
            cur = conn.cursor()
            cur.execute("UPDATE user_mitsumori SET status = 'approved' WHERE id = ?", (id,))
        conn.commit()
        cur.close()
        conn.close()
        flash('見積依頼書を承認しました', 'success')
    except Exception as e:
        flash(f'承認エラー: {str(e)}', 'error')
    return redirect(url_for('admin_mitsumori_list'))

@app.route('/admin/mitsumori/<int:id>/reject', methods=['POST'])
@login_required
@admin_required
def admin_mitsumori_reject(id):
    """見積依頼書却下"""
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute("UPDATE user_mitsumori SET status = 'rejected' WHERE id = %s", (id,))
        else:
            cur = conn.cursor()
            cur.execute("UPDATE user_mitsumori SET status = 'rejected' WHERE id = ?", (id,))
        conn.commit()
        cur.close()
        conn.close()
        flash('見積依頼書を却下しました', 'info')
    except Exception as e:
        flash(f'却下エラー: {str(e)}', 'error')
    return redirect(url_for('admin_mitsumori_list'))

@app.route('/admin/mitsumori/<int:id>/pdf')
@login_required
@admin_required
def admin_mitsumori_pdf(id):
    """見積依頼書PDF"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM user_mitsumori WHERE id = %s", (id,))
        mitsumori = cur.fetchone()
        if mitsumori:
            mitsumori = dict(mitsumori)
            cur.execute("SELECT * FROM user_mitsumori_items WHERE mitsumori_id = %s ORDER BY item_no", (id,))
            items = [dict(row) for row in cur.fetchall()]
        else:
            items = []
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM user_mitsumori WHERE id = ?", (id,))
        row = cur.fetchone()
        if row:
            columns = [d[0] for d in cur.description]
            mitsumori = dict(zip(columns, row))
            cur.execute("SELECT * FROM user_mitsumori_items WHERE mitsumori_id = ? ORDER BY item_no", (id,))
            item_columns = [d[0] for d in cur.description] if cur.description else []
            items = [dict(zip(item_columns, r)) for r in cur.fetchall()]
        else:
            mitsumori = None
            items = []
    
    cur.close()
    conn.close()
    
    if not mitsumori:
        flash('見積依頼書が見つかりません', 'error')
        return redirect(url_for('admin_mitsumori_list'))
    
    html_content = render_template('pdf/mitsumori_pdf.html', mitsumori=mitsumori, items=items)
    return html_content, 200, {'Content-Type': 'text/html; charset=utf-8'}

# ===================
# LINE公式アカウント連携
# ===================

def generate_weekly_report():
    """週次レポートを生成"""
    from datetime import datetime, timedelta
    
    # 今週の月曜日と日曜日を計算
    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # 今週の売上・仕入れを集計
        cur.execute("""
            SELECT 
                COUNT(*) as total_items,
                COALESCE(SUM(CASE WHEN status = '販売済' THEN 1 ELSE 0 END), 0) as sold_items,
                COALESCE(SUM(purchase_price), 0) as total_purchase,
                COALESCE(SUM(CASE WHEN status = '販売済' THEN selling_price ELSE 0 END), 0) as total_sales,
                COALESCE(SUM(CASE WHEN status = '販売済' THEN selling_price - purchase_price ELSE 0 END), 0) as total_profit
            FROM merchandise
            WHERE created_at >= %s AND created_at <= %s
        """, (monday.strftime('%Y-%m-%d'), sunday.strftime('%Y-%m-%d 23:59:59')))
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                COUNT(*) as total_items,
                COALESCE(SUM(CASE WHEN status = '販売済' THEN 1 ELSE 0 END), 0) as sold_items,
                COALESCE(SUM(purchase_price), 0) as total_purchase,
                COALESCE(SUM(CASE WHEN status = '販売済' THEN selling_price ELSE 0 END), 0) as total_sales,
                COALESCE(SUM(CASE WHEN status = '販売済' THEN selling_price - purchase_price ELSE 0 END), 0) as total_profit
            FROM merchandise
            WHERE created_at >= ? AND created_at <= ?
        """, (monday.strftime('%Y-%m-%d'), sunday.strftime('%Y-%m-%d 23:59:59')))
    
    row = cur.fetchone()
    cur.close()
    conn.close()
    
    if DATABASE_URL:
        data = row
    else:
        data = {
            'total_items': row[0] or 0,
            'sold_items': row[1] or 0,
            'total_purchase': row[2] or 0,
            'total_sales': row[3] or 0,
            'total_profit': row[4] or 0
        }
    
    report = f"""期間: {monday.strftime('%m/%d')} - {sunday.strftime('%m/%d')}

・新規登録数: {data['total_items']}件
・販売数: {data['sold_items']}件
・総仕入額: ¥{int(data['total_purchase']):,}
・総売上: ¥{int(data['total_sales']):,}
・総利益: ¥{int(data['total_profit']):,}"""
    
    return report

def generate_monthly_report():
    """月次レポートを生成"""
    from datetime import datetime
    import calendar
    
    # 先月の期間を計算
    today = datetime.now()
    if today.month == 1:
        last_month = 12
        last_year = today.year - 1
    else:
        last_month = today.month - 1
        last_year = today.year
    
    first_day = f"{last_year}-{last_month:02d}-01"
    last_day_num = calendar.monthrange(last_year, last_month)[1]
    last_day = f"{last_year}-{last_month:02d}-{last_day_num}"
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT 
                COUNT(*) as total_items,
                COALESCE(SUM(CASE WHEN status = '販売済' THEN 1 ELSE 0 END), 0) as sold_items,
                COALESCE(SUM(purchase_price), 0) as total_purchase,
                COALESCE(SUM(CASE WHEN status = '販売済' THEN selling_price ELSE 0 END), 0) as total_sales,
                COALESCE(SUM(CASE WHEN status = '販売済' THEN selling_price - purchase_price ELSE 0 END), 0) as total_profit
            FROM merchandise
            WHERE created_at >= %s AND created_at <= %s
        """, (first_day, last_day + ' 23:59:59'))
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                COUNT(*) as total_items,
                COALESCE(SUM(CASE WHEN status = '販売済' THEN 1 ELSE 0 END), 0) as sold_items,
                COALESCE(SUM(purchase_price), 0) as total_purchase,
                COALESCE(SUM(CASE WHEN status = '販売済' THEN selling_price ELSE 0 END), 0) as total_sales,
                COALESCE(SUM(CASE WHEN status = '販売済' THEN selling_price - purchase_price ELSE 0 END), 0) as total_profit
            FROM merchandise
            WHERE created_at >= ? AND created_at <= ?
        """, (first_day, last_day + ' 23:59:59'))
    
    row = cur.fetchone()
    cur.close()
    conn.close()
    
    if DATABASE_URL:
        data = row
    else:
        data = {
            'total_items': row[0] or 0,
            'sold_items': row[1] or 0,
            'total_purchase': row[2] or 0,
            'total_sales': row[3] or 0,
            'total_profit': row[4] or 0
        }
    
    month_names = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月']
    
    report = f"""対象月: {last_year}年{month_names[last_month-1]}

・新規登録数: {data['total_items']}件
・販売数: {data['sold_items']}件
・総仕入額: ¥{int(data['total_purchase']):,}
・総売上: ¥{int(data['total_sales']):,}
・総利益: ¥{int(data['total_profit']):,}"""
    
    return report

def generate_monthly_fee_report():
    """月謝利用料金のお知らせを生成"""
    from datetime import datetime
    
    today = datetime.now()
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # ユーザーごとの利用状況を取得
        cur.execute("""
            SELECT 
                u.display_name,
                u.subscription_status,
                COUNT(m.id) as item_count,
                sp.name as plan_name,
                sp.base_price,
                sp.per_item_price
            FROM users u
            LEFT JOIN merchandise m ON m.user_id = u.id
            LEFT JOIN subscription_plans sp ON u.subscription_status = 'active'
            WHERE u.role IN ('owner', 'admin')
            GROUP BY u.id, u.display_name, u.subscription_status, sp.name, sp.base_price, sp.per_item_price
        """)
        users = cur.fetchall()
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                u.display_name,
                u.subscription_status,
                COUNT(m.id) as item_count
            FROM users u
            LEFT JOIN merchandise m ON m.user_id = u.id
            WHERE u.role IN ('owner', 'admin')
            GROUP BY u.id
        """)
        users = cur.fetchall()
    
    cur.close()
    conn.close()
    
    month_names = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月']
    
    report = f"""対象月: {today.year}年{month_names[today.month-1]}

ご利用料金の詳細は管理画面の「決済管理」よりご確認いただけます。

ご不明な点がございましたら、お気軽にお問い合わせください。"""
    
    return report

def send_line_broadcast(message):
    """LINE一斉送信"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM line_settings LIMIT 1")
        settings = cur.fetchone()
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM line_settings LIMIT 1")
        row = cur.fetchone()
        settings = dict(zip(['id', 'channel_access_token', 'channel_secret', 'is_enabled', 'updated_at'], row)) if row else None
    cur.close()
    conn.close()
    
    if not settings or not settings.get('channel_access_token'):
        return {'success': False, 'error': 'LINE設定が完了していません'}
    
    import requests
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f"Bearer {settings['channel_access_token']}"
    }
    data = {
        'messages': [{'type': 'text', 'text': message}]
    }
    
    try:
        response = requests.post(
            'https://api.line.me/v2/bot/message/broadcast',
            headers=headers,
            json=data
        )
        if response.status_code == 200:
            return {'success': True}
        else:
            return {'success': False, 'error': response.text}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def send_line_push(user_id, message):
    """LINE個別送信"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM line_settings LIMIT 1")
        settings = cur.fetchone()
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM line_settings LIMIT 1")
        row = cur.fetchone()
        settings = dict(zip(['id', 'channel_access_token', 'channel_secret', 'is_enabled', 'updated_at'], row)) if row else None
    cur.close()
    conn.close()
    
    if not settings or not settings.get('channel_access_token'):
        return {'success': False, 'error': 'LINE設定が完了していません'}
    
    import requests
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f"Bearer {settings['channel_access_token']}"
    }
    data = {
        'to': user_id,
        'messages': [{'type': 'text', 'text': message}]
    }
    
    try:
        response = requests.post(
            'https://api.line.me/v2/bot/message/push',
            headers=headers,
            json=data
        )
        if response.status_code == 200:
            return {'success': True}
        else:
            return {'success': False, 'error': response.text}
    except Exception as e:
        return {'success': False, 'error': str(e)}

@app.route('/admin/line')
@login_required
def admin_line_dashboard():
    """LINE連携ダッシュボード"""
    if not current_user.is_owner():
        flash('この機能はオーナーのみ利用可能です', 'error')
        return redirect(url_for('index'))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM line_settings LIMIT 1")
        settings = cur.fetchone()
        
        # LINE連携済みユーザー数
        cur.execute("SELECT COUNT(*) as count FROM users WHERE line_user_id IS NOT NULL")
        linked_users = cur.fetchone()['count']
        
        # 定期送信メッセージ
        cur.execute("SELECT * FROM line_scheduled_messages ORDER BY id DESC")
        scheduled_messages = cur.fetchall()
        
        # 送信履歴
        cur.execute("""
            SELECT l.*, u.display_name as sent_by_name
            FROM line_message_logs l
            LEFT JOIN users u ON l.sent_by = u.id
            ORDER BY l.sent_at DESC LIMIT 20
        """)
        logs = cur.fetchall()
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM line_settings LIMIT 1")
        row = cur.fetchone()
        settings = dict(zip(['id', 'channel_access_token', 'channel_secret', 'is_enabled', 'updated_at'], row)) if row else {}
        
        cur.execute("SELECT COUNT(*) FROM users WHERE line_user_id IS NOT NULL")
        linked_users = cur.fetchone()[0]
        
        cur.execute("SELECT * FROM line_scheduled_messages ORDER BY id DESC")
        scheduled_messages = cur.fetchall()
        
        cur.execute("""
            SELECT l.*, u.display_name as sent_by_name
            FROM line_message_logs l
            LEFT JOIN users u ON l.sent_by = u.id
            ORDER BY l.sent_at DESC LIMIT 20
        """)
        logs = cur.fetchall()
    
    cur.close()
    conn.close()
    
    return render_template('admin/line_dashboard.html',
                         settings=dict(settings) if settings else {},
                         linked_users=linked_users,
                         scheduled_messages=[dict(m) for m in scheduled_messages] if scheduled_messages else [],
                         logs=[dict(l) for l in logs] if logs else [])

@app.route('/admin/line/settings', methods=['POST'])
@login_required
def admin_line_settings():
    """LINE設定を保存"""
    if not current_user.is_owner():
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    
    channel_access_token = request.form.get('channel_access_token', '')
    channel_secret = request.form.get('channel_secret', '')
    is_enabled = request.form.get('is_enabled') == 'on'
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("""
            UPDATE line_settings 
            SET channel_access_token = %s, channel_secret = %s, is_enabled = %s, updated_at = CURRENT_TIMESTAMP
        """, (channel_access_token, channel_secret, is_enabled))
    else:
        cur = conn.cursor()
        cur.execute("""
            UPDATE line_settings 
            SET channel_access_token = ?, channel_secret = ?, is_enabled = ?, updated_at = CURRENT_TIMESTAMP
        """, (channel_access_token, channel_secret, 1 if is_enabled else 0))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('LINE設定を保存しました', 'success')
    return redirect(url_for('admin_line_dashboard'))

@app.route('/admin/line/broadcast', methods=['POST'])
@login_required
def admin_line_broadcast():
    """LINE一斉送信"""
    if not current_user.is_owner():
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    
    message = request.form.get('message', '').strip()
    if not message:
        flash('メッセージを入力してください', 'error')
        return redirect(url_for('admin_line_dashboard'))
    
    result = send_line_broadcast(message)
    
    # ログを記録
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO line_message_logs (message_type, message_content, target_count, success_count, sent_by)
            VALUES (%s, %s, %s, %s, %s)
        """, ('broadcast', message, 0, 1 if result['success'] else 0, current_user.id))
    else:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO line_message_logs (message_type, message_content, target_count, success_count, sent_by)
            VALUES (?, ?, ?, ?, ?)
        """, ('broadcast', message, 0, 1 if result['success'] else 0, current_user.id))
    conn.commit()
    cur.close()
    conn.close()
    
    if result['success']:
        flash('メッセージを送信しました', 'success')
    else:
        flash(f"送信エラー: {result.get('error', '不明なエラー')}", 'error')
    
    return redirect(url_for('admin_line_dashboard'))

@app.route('/admin/line/scheduled', methods=['GET', 'POST'])
@login_required
def admin_line_scheduled():
    """定期送信メッセージ管理"""
    if not current_user.is_owner():
        flash('この機能はオーナーのみ利用可能です', 'error')
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        message_content = request.form.get('message_content', '').strip()
        schedule_type = request.form.get('schedule_type', 'daily')
        schedule_time = request.form.get('schedule_time', '09:00')
        schedule_day = request.form.get('schedule_day', 1)
        is_enabled = request.form.get('is_enabled') == 'on'
        
        if not title or not message_content:
            flash('タイトルとメッセージ内容を入力してください', 'error')
            return redirect(url_for('admin_line_scheduled'))
        
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO line_scheduled_messages (title, message_content, schedule_type, schedule_time, schedule_day, is_enabled)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (title, message_content, schedule_type, schedule_time, int(schedule_day), is_enabled))
        else:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO line_scheduled_messages (title, message_content, schedule_type, schedule_time, schedule_day, is_enabled)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (title, message_content, schedule_type, schedule_time, int(schedule_day), 1 if is_enabled else 0))
        conn.commit()
        cur.close()
        conn.close()
        
        flash('定期送信メッセージを登録しました', 'success')
        return redirect(url_for('admin_line_dashboard'))
    
    return render_template('admin/line_scheduled_form.html')

@app.route('/admin/line/scheduled/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def admin_line_scheduled_edit(id):
    """定期送信メッセージ編集"""
    if not current_user.is_owner():
        flash('この機能はオーナーのみ利用可能です', 'error')
        return redirect(url_for('index'))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()
    
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        message_content = request.form.get('message_content', '').strip()
        schedule_type = request.form.get('schedule_type', 'daily')
        schedule_time = request.form.get('schedule_time', '09:00')
        schedule_day = request.form.get('schedule_day', 1)
        is_enabled = request.form.get('is_enabled') == 'on'
        
        if DATABASE_URL:
            cur.execute("""
                UPDATE line_scheduled_messages 
                SET title = %s, message_content = %s, schedule_type = %s, schedule_time = %s, 
                    schedule_day = %s, is_enabled = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (title, message_content, schedule_type, schedule_time, int(schedule_day), is_enabled, id))
        else:
            cur.execute("""
                UPDATE line_scheduled_messages 
                SET title = ?, message_content = ?, schedule_type = ?, schedule_time = ?, 
                    schedule_day = ?, is_enabled = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (title, message_content, schedule_type, schedule_time, int(schedule_day), 1 if is_enabled else 0, id))
        conn.commit()
        cur.close()
        conn.close()
        
        flash('定期送信メッセージを更新しました', 'success')
        return redirect(url_for('admin_line_dashboard'))
    
    if DATABASE_URL:
        cur.execute("SELECT * FROM line_scheduled_messages WHERE id = %s", (id,))
    else:
        cur.execute("SELECT * FROM line_scheduled_messages WHERE id = ?", (id,))
    message = cur.fetchone()
    cur.close()
    conn.close()
    
    if not message:
        flash('メッセージが見つかりません', 'error')
        return redirect(url_for('admin_line_dashboard'))
    
    return render_template('admin/line_scheduled_form.html', message=dict(message))

@app.route('/admin/line/scheduled/<int:id>/delete', methods=['POST'])
@login_required
def admin_line_scheduled_delete(id):
    """定期送信メッセージ削除"""
    if not current_user.is_owner():
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("DELETE FROM line_scheduled_messages WHERE id = %s", (id,))
    else:
        cur = conn.cursor()
        cur.execute("DELETE FROM line_scheduled_messages WHERE id = ?", (id,))
    conn.commit()
    cur.close()
    conn.close()
    
    flash('定期送信メッセージを削除しました', 'success')
    return redirect(url_for('admin_line_dashboard'))

@app.route('/admin/line/scheduled/<int:id>/toggle', methods=['POST'])
@login_required
def admin_line_scheduled_toggle(id):
    """定期送信メッセージの有効/無効切り替え"""
    if not current_user.is_owner():
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT is_enabled FROM line_scheduled_messages WHERE id = %s", (id,))
        msg = cur.fetchone()
        if msg:
            new_value = not msg['is_enabled']
            cur.execute("UPDATE line_scheduled_messages SET is_enabled = %s WHERE id = %s", (new_value, id))
    else:
        cur = conn.cursor()
        cur.execute("SELECT is_enabled FROM line_scheduled_messages WHERE id = ?", (id,))
        msg = cur.fetchone()
        if msg:
            new_value = 0 if msg[0] else 1
            cur.execute("UPDATE line_scheduled_messages SET is_enabled = ? WHERE id = ?", (new_value, id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    return jsonify({'success': True})

@app.route('/admin/line/preview-report', methods=['POST'])
@login_required
def admin_line_preview_report():
    """レポートのプレビュー生成"""
    if not current_user.is_owner():
        return jsonify({'success': False, 'error': '権限がありません'}), 403
    
    data = request.get_json()
    message_content = data.get('message_content', '')
    report_type = data.get('report_type', '')
    
    try:
        if report_type == 'weekly_report':
            report_data = generate_weekly_report()
            preview = message_content.replace('{weekly_report}', report_data)
        elif report_type == 'monthly_report':
            report_data = generate_monthly_report()
            preview = message_content.replace('{monthly_report}', report_data)
        elif report_type == 'monthly_fee':
            report_data = generate_monthly_fee_report()
            preview = message_content.replace('{monthly_fee}', report_data)
        else:
            preview = message_content
        
        return jsonify({'success': True, 'preview': preview})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/line/webhook', methods=['POST'])
def line_webhook():
    """LINE Webhookエンドポイント（友だち追加時などにuser_idを取得）"""
    import hmac
    import hashlib
    import base64
    
    # 署名検証
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT channel_secret FROM line_settings LIMIT 1")
        settings = cur.fetchone()
    else:
        cur = conn.cursor()
        cur.execute("SELECT channel_secret FROM line_settings LIMIT 1")
        row = cur.fetchone()
        settings = {'channel_secret': row[0]} if row else None
    cur.close()
    conn.close()
    
    if not settings or not settings.get('channel_secret'):
        return 'OK', 200
    
    body = request.get_data(as_text=True)
    signature = request.headers.get('X-Line-Signature', '')
    
    hash = hmac.new(settings['channel_secret'].encode('utf-8'), body.encode('utf-8'), hashlib.sha256).digest()
    expected_signature = base64.b64encode(hash).decode('utf-8')
    
    if signature != expected_signature:
        return 'Invalid signature', 400
    
    # イベント処理
    data = request.get_json()
    for event in data.get('events', []):
        if event.get('type') == 'follow':
            # 友だち追加時
            user_id = event.get('source', {}).get('userId')
            if user_id:
                # 後でユーザーと紐付ける処理を追加可能
                pass
    
    return 'OK', 200

# 定期送信実行関数
def run_scheduled_line_messages():
    """定期送信メッセージを実行"""
    from datetime import datetime
    now = datetime.now()
    current_time = now.strftime('%H:%M')
    current_day = now.day
    current_weekday = now.weekday()  # 0=月曜
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT * FROM line_scheduled_messages 
            WHERE is_enabled = TRUE AND schedule_time = %s
        """, (current_time,))
        messages = cur.fetchall()
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM line_scheduled_messages 
            WHERE is_enabled = 1 AND schedule_time = ?
        """, (current_time,))
        messages = cur.fetchall()
    
    for msg in messages:
        msg_dict = dict(msg) if not isinstance(msg, dict) else msg
        should_send = False
        
        schedule_type = msg_dict.get('schedule_type', 'daily')
        schedule_day = msg_dict.get('schedule_day', 1)
        
        if schedule_type == 'daily':
            should_send = True
        elif schedule_type == 'weekly' and current_weekday == (schedule_day - 1):
            should_send = True
        elif schedule_type == 'monthly' and current_day == schedule_day:
            should_send = True
        
        if should_send:
            # メッセージ内容を取得
            message_content = msg_dict['message_content']
            report_type = msg_dict.get('report_type')
            
            # レポートタイプに応じてデータを自動生成
            if report_type == 'weekly_report':
                try:
                    weekly_data = generate_weekly_report()
                    message_content = message_content.replace('{weekly_report}', weekly_data)
                except Exception as e:
                    print(f"週次レポート生成エラー: {e}")
            elif report_type == 'monthly_report':
                try:
                    monthly_data = generate_monthly_report()
                    message_content = message_content.replace('{monthly_report}', monthly_data)
                except Exception as e:
                    print(f"月次レポート生成エラー: {e}")
            elif report_type == 'monthly_fee':
                try:
                    fee_data = generate_monthly_fee_report()
                    message_content = message_content.replace('{monthly_fee}', fee_data)
                except Exception as e:
                    print(f"月謝料金レポート生成エラー: {e}")
            
            result = send_line_broadcast(message_content)
            
            # last_sent_atを更新
            if DATABASE_URL:
                cur.execute("UPDATE line_scheduled_messages SET last_sent_at = CURRENT_TIMESTAMP WHERE id = %s", (msg_dict['id'],))
                cur.execute("""
                    INSERT INTO line_message_logs (message_type, message_content, success_count)
                    VALUES (%s, %s, %s)
                """, ('scheduled', message_content, 1 if result['success'] else 0))
            else:
                cur.execute("UPDATE line_scheduled_messages SET last_sent_at = CURRENT_TIMESTAMP WHERE id = ?", (msg_dict['id'],))
                cur.execute("""
                    INSERT INTO line_message_logs (message_type, message_content, success_count)
                    VALUES (?, ?, ?)
                """, ('scheduled', message_content, 1 if result['success'] else 0))
    
    conn.commit()
    cur.close()
    conn.close()


def check_and_transfer_overdue_items():
    """3ヶ月以上未払いのユーザーの商品を管理者に自動移動"""
    print(f"[{datetime.now()}] Running overdue items check...")
    
    conn = get_db()
    
    # 90日（約3ヶ月）以上未払いのユーザーを取得
    three_months_ago = datetime.now() - timedelta(days=90)
    
    if DATABASE_URL:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # 3ヶ月以上未払いのユーザーを取得
        cur.execute("""
            SELECT id, display_name, username, line_user_id, email 
            FROM users 
            WHERE subscription_status = 'past_due' 
              AND overdue_since IS NOT NULL 
              AND overdue_since <= %s
        """, (three_months_ago,))
        overdue_users = [dict(row) for row in cur.fetchall()]
        
        # オーナーのIDを取得
        cur.execute("SELECT id FROM users WHERE role = 'owner' LIMIT 1")
        owner = cur.fetchone()
        owner_id = owner['id'] if owner else 1
    else:
        cur = conn.cursor()
        
        cur.execute("""
            SELECT id, display_name, username, line_user_id, email 
            FROM users 
            WHERE subscription_status = 'past_due' 
              AND overdue_since IS NOT NULL 
              AND overdue_since <= ?
        """, (three_months_ago.strftime('%Y-%m-%d %H:%M:%S'),))
        
        columns = ['id', 'display_name', 'username', 'line_user_id', 'email']
        overdue_users = [dict(zip(columns, row)) for row in cur.fetchall()]
        
        cur.execute("SELECT id FROM users WHERE role = 'owner' LIMIT 1")
        owner = cur.fetchone()
        owner_id = owner[0] if owner else 1
    
    transferred_count = 0
    
    for user in overdue_users:
        user_id = user['id']
        display_name = user.get('display_name') or user.get('username') or 'ユーザー'
        line_user_id = user.get('line_user_id')
        
        # 移動対象の商品数を取得
        if DATABASE_URL:
            cur.execute("""
                SELECT COUNT(*) as count FROM merchandise 
                WHERE user_id = %s AND sale_date IS NULL
            """, (user_id,))
            item_count = cur.fetchone()['count']
        else:
            cur.execute("""
                SELECT COUNT(*) FROM merchandise 
                WHERE user_id = ? AND sale_date IS NULL
            """, (user_id,))
            item_count = cur.fetchone()[0]
        
        if item_count > 0:
            # 商品を管理者に移動
            transfer_note = f'[自動移管 {datetime.now().strftime("%Y/%m/%d")}] {display_name}様から未払い3ヶ月超過により移管'
            
            if DATABASE_URL:
                cur.execute("""
                    UPDATE merchandise 
                    SET user_id = %s, 
                        notes = COALESCE(notes, '') || E'\\n' || %s
                    WHERE user_id = %s AND sale_date IS NULL
                """, (owner_id, transfer_note, user_id))
            else:
                cur.execute("""
                    UPDATE merchandise 
                    SET user_id = ?, 
                        notes = COALESCE(notes, '') || char(10) || ?
                    WHERE user_id = ? AND sale_date IS NULL
                """, (owner_id, transfer_note, user_id))
            
            transferred_count += item_count
            
            # LINE通知を送信
            if line_user_id:
                message = f"""⚠️ 商品の自動移管完了のお知らせ

{display_name}様

3ヶ月以上の未払いにより、お客様の在庫商品（{item_count}点）は管理者へ移管されました。

移管された商品は、弊社にて適切に処理いたします。

ご不明点がございましたら、お問い合わせください。"""
                try:
                    send_line_push(line_user_id, message)
                except Exception as e:
                    print(f"LINE notification error for user {user_id}: {e}")
    
    conn.commit()
    cur.close()
    conn.close()
    
    print(f"[{datetime.now()}] Overdue items check completed. Transferred {transferred_count} items from {len(overdue_users)} users.")


def check_and_transfer_long_term_items():
    """仕入れ日から5ヶ月以上経過した長期在庫商品を管理者に自動移動"""
    print(f"[{datetime.now()}] Running long-term items check...")
    
    conn = get_db()
    
    # 150日（約5ヶ月）以上経過
    five_months_ago = datetime.now() - timedelta(days=150)
    five_months_ago_str = five_months_ago.strftime('%Y-%m-%d')
    
    if DATABASE_URL:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # オーナーのIDを取得
        cur.execute("SELECT id FROM users WHERE role = 'owner' LIMIT 1")
        owner = cur.fetchone()
        owner_id = owner['id'] if owner else 1
        
        # 仕入れ日から5ヶ月以上経過した未売却の一般ユーザー商品を取得
        cur.execute("""
            SELECT m.id, m.user_id, m.product_name, u.display_name, u.username, u.line_user_id
            FROM merchandise m
            JOIN users u ON m.user_id = u.id
            WHERE m.sale_date IS NULL
              AND m.purchase_date IS NOT NULL
              AND m.purchase_date <= %s
              AND u.role = 'user'
        """, (five_months_ago_str,))
        long_term_items = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        
        # オーナーのIDを取得
        cur.execute("SELECT id FROM users WHERE role = 'owner' LIMIT 1")
        owner = cur.fetchone()
        owner_id = owner[0] if owner else 1
        
        cur.execute("""
            SELECT m.id, m.user_id, m.product_name, u.display_name, u.username, u.line_user_id
            FROM merchandise m
            JOIN users u ON m.user_id = u.id
            WHERE m.sale_date IS NULL
              AND m.purchase_date IS NOT NULL
              AND m.purchase_date <= ?
              AND u.role = 'user'
        """, (five_months_ago_str,))
        
        columns = ['id', 'user_id', 'product_name', 'display_name', 'username', 'line_user_id']
        long_term_items = [dict(zip(columns, row)) for row in cur.fetchall()]
    
    if not long_term_items:
        print(f"[{datetime.now()}] No long-term items to transfer.")
        cur.close()
        conn.close()
        return
    
    # ユーザーごとにグループ化
    users_items = {}
    for item in long_term_items:
        user_id = item['user_id']
        if user_id not in users_items:
            users_items[user_id] = {
                'display_name': item.get('display_name') or item.get('username') or 'ユーザー',
                'line_user_id': item.get('line_user_id'),
                'items': []
            }
        users_items[user_id]['items'].append(item)
    
    transferred_count = 0
    
    for user_id, user_data in users_items.items():
        items = user_data['items']
        display_name = user_data['display_name']
        line_user_id = user_data['line_user_id']
        
        # 商品を管理者に移動
        item_ids = [item['id'] for item in items]
        
        if DATABASE_URL:
            cur.execute("""
                UPDATE merchandise 
                SET user_id = %s, 
                    notes = COALESCE(notes, '') || '[長期在庫自動移管] ' || %s || 'さんから移管 (' || to_char(NOW(), 'YYYY/MM/DD') || ') '
                WHERE id = ANY(%s)
            """, (owner_id, display_name, item_ids))
        else:
            for item_id in item_ids:
                cur.execute("""
                    UPDATE merchandise 
                    SET user_id = ?,
                        notes = COALESCE(notes, '') || '[長期在庫自動移管] ' || ? || 'さんから移管 (' || date('now') || ') '
                    WHERE id = ?
                """, (owner_id, display_name, item_id))
        
        transferred_count += len(items)
        
        # LINEで通知
        if line_user_id:
            try:
                item_names = [item.get('product_name', '商品') for item in items[:5]]
                items_text = '\n'.join([f'・{name}' for name in item_names])
                if len(items) > 5:
                    items_text += f'\n...他{len(items) - 5}件'
                
                message = f"""【長期在庫商品の自動移管のお知らせ】

{display_name}様

仕入れ日から5ヶ月以上経過した商品が管理者へ移管されました。

移管商品（{len(items)}件）:
{items_text}

今後の商品の取り扱いについては、管理者にお問い合わせください。"""
                
                send_line_push(line_user_id, message)
            except Exception as e:
                print(f"LINE notification failed for user {user_id}: {e}")
    
    conn.commit()
    cur.close()
    conn.close()
    
    print(f"[{datetime.now()}] Long-term items check completed. Transferred {transferred_count} items from {len(users_items)} users.")


# ===================
# 未払いユーザー向け商品処分
# ===================

@app.route('/disposal-options')
@login_required
def disposal_options():
    """未払いユーザー向け商品処分オプションページ"""
    # 支払い遅延中のユーザーのみアクセス可能
    if current_user.subscription_status != 'past_due':
        flash('このページにアクセスする権限がありません', 'error')
        return redirect(url_for('index'))
    
    items = []
    user_info = {}
    
    try:
        conn = get_db()
        
        if DATABASE_URL:
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            # 未払い期間を取得
            cur.execute("""
                SELECT overdue_since FROM users WHERE id = %s
            """, (current_user.id,))
            user_info = cur.fetchone() or {}
            if user_info:
                user_info = dict(user_info)
            
            # ユーザーの商品一覧を取得
            cur.execute("""
                SELECT m.*, 
                       dr.id as disposal_request_id, dr.disposal_type, dr.status as disposal_status
                FROM merchandise m
                LEFT JOIN item_disposal_requests dr ON m.id = dr.merchandise_id AND dr.status != 'completed'
                WHERE m.user_id = %s AND m.sale_date IS NULL
                ORDER BY m.created_at DESC
            """, (current_user.id,))
            items = [dict(row) for row in cur.fetchall()]
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            # 未払い期間を取得
            cur.execute("""
                SELECT overdue_since FROM users WHERE id = ?
            """, (current_user.id,))
            result = cur.fetchone()
            user_info = {'overdue_since': result[0]} if result else {}
            
            # ユーザーの商品一覧を取得
            cur.execute("""
                SELECT m.*, 
                       dr.id as disposal_request_id, dr.disposal_type, dr.status as disposal_status
                FROM merchandise m
                LEFT JOIN item_disposal_requests dr ON m.id = dr.merchandise_id AND dr.status != 'completed'
                WHERE m.user_id = ? AND m.sale_date IS NULL
                ORDER BY m.created_at DESC
            """, (current_user.id,))
            items = [dict(row) for row in cur.fetchall()]
        
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] disposal_options: {e}", flush=True)
    
    # 未払い期間を計算
    overdue_since = user_info.get('overdue_since') if user_info else None
    overdue_days = 0
    overdue_months = 0
    
    if overdue_since:
        if isinstance(overdue_since, str):
            try:
                overdue_since = datetime.strptime(overdue_since, '%Y-%m-%d %H:%M:%S')
            except:
                try:
                    overdue_since = datetime.strptime(overdue_since, '%Y-%m-%d %H:%M:%S.%f')
                except:
                    overdue_since = None
        
        if overdue_since:
            overdue_days = (datetime.now() - overdue_since).days
            overdue_months = overdue_days // 30
    
    return render_template('disposal_options.html',
                           items=items,
                           overdue_days=overdue_days,
                           overdue_months=overdue_months,
                           overdue_since=overdue_since)


@app.route('/disposal-request', methods=['POST'])
@login_required
def submit_disposal_request():
    """商品処分申請を送信"""
    if current_user.subscription_status != 'past_due':
        return jsonify({'success': False, 'error': 'アクセス権限がありません'}), 403
    
    disposal_type = request.form.get('disposal_type')
    merchandise_ids = request.form.getlist('merchandise_ids[]')
    shipping_address = request.form.get('shipping_address', '')
    shipping_name = request.form.get('shipping_name', '')
    shipping_phone = request.form.get('shipping_phone', '')
    
    if not disposal_type or not merchandise_ids:
        flash('処分方法と商品を選択してください', 'error')
        return redirect(url_for('disposal_options'))
    
    if disposal_type not in ['auction', 'liquidation', 'shipping']:
        flash('無効な処分方法です', 'error')
        return redirect(url_for('disposal_options'))
    
    # 郵送の場合は住所必須
    if disposal_type == 'shipping' and not shipping_address:
        flash('郵送先住所を入力してください', 'error')
        return redirect(url_for('disposal_options'))
    
    conn = get_db()
    cur = conn.cursor()
    
    try:
        for merchandise_id in merchandise_ids:
            # 商品がユーザーのものか確認
            if DATABASE_URL:
                cur.execute("SELECT id FROM merchandise WHERE id = %s AND user_id = %s", 
                           (merchandise_id, current_user.id))
            else:
                cur.execute("SELECT id FROM merchandise WHERE id = ? AND user_id = ?", 
                           (merchandise_id, current_user.id))
            
            if cur.fetchone():
                # 既存の申請を確認
                if DATABASE_URL:
                    cur.execute("""
                        SELECT id FROM item_disposal_requests 
                        WHERE merchandise_id = %s AND status = 'pending'
                    """, (merchandise_id,))
                else:
                    cur.execute("""
                        SELECT id FROM item_disposal_requests 
                        WHERE merchandise_id = ? AND status = 'pending'
                    """, (merchandise_id,))
                
                existing = cur.fetchone()
                
                if existing:
                    # 既存の申請を更新
                    if DATABASE_URL:
                        cur.execute("""
                            UPDATE item_disposal_requests 
                            SET disposal_type = %s, shipping_address = %s, 
                                shipping_name = %s, shipping_phone = %s
                            WHERE id = %s
                        """, (disposal_type, shipping_address, shipping_name, shipping_phone, existing[0]))
                    else:
                        cur.execute("""
                            UPDATE item_disposal_requests 
                            SET disposal_type = ?, shipping_address = ?, 
                                shipping_name = ?, shipping_phone = ?
                            WHERE id = ?
                        """, (disposal_type, shipping_address, shipping_name, shipping_phone, existing[0]))
                else:
                    # 新規申請を作成
                    if DATABASE_URL:
                        cur.execute("""
                            INSERT INTO item_disposal_requests 
                            (user_id, merchandise_id, disposal_type, shipping_address, shipping_name, shipping_phone)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (current_user.id, merchandise_id, disposal_type, 
                              shipping_address, shipping_name, shipping_phone))
                    else:
                        cur.execute("""
                            INSERT INTO item_disposal_requests 
                            (user_id, merchandise_id, disposal_type, shipping_address, shipping_name, shipping_phone)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (current_user.id, merchandise_id, disposal_type, 
                              shipping_address, shipping_name, shipping_phone))
        
        conn.commit()
        
        disposal_type_names = {
            'auction': 'オークション販売',
            'liquidation': '在庫処分',
            'shipping': '受け取り郵送'
        }
        flash(f'{len(merchandise_ids)}件の商品を「{disposal_type_names.get(disposal_type)}」で申請しました', 'success')
        
    except Exception as e:
        conn.rollback()
        flash(f'エラーが発生しました: {str(e)}', 'error')
    finally:
        cur.close()
        conn.close()
    
    return redirect(url_for('disposal_options'))


@app.route('/long-term-items')
@login_required
def long_term_items():
    """長期在庫商品（仕入れ日から3ヶ月以上経過）の一覧・処分オプションページ"""
    items = []
    
    try:
        conn = get_db()
        
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            # デバッグ: まず条件に合う商品数を確認
            cur.execute("""
                SELECT COUNT(*) as count
                FROM merchandise m
                WHERE m.user_id = %s 
                  AND m.sale_date IS NULL
                  AND m.purchase_date IS NOT NULL
                  AND m.purchase_date <= (CURRENT_DATE - INTERVAL '90 days')::DATE
            """, (current_user.id,))
            debug_count = cur.fetchone()['count']
            print(f"[DEBUG] long_term_items: Found {debug_count} items matching date condition (user_id={current_user.id})", flush=True)
            
            # 通知マークのカウントと一致させるため、申請未作成の商品も確認
            cur.execute("""
                SELECT COUNT(*) as count
                FROM merchandise m
                LEFT JOIN item_disposal_requests dr ON m.id = dr.merchandise_id AND dr.status != 'completed' AND dr.reason = 'long_term'
                WHERE m.user_id = %s 
                  AND m.sale_date IS NULL
                  AND m.purchase_date IS NOT NULL
                  AND m.purchase_date <= (CURRENT_DATE - INTERVAL '90 days')::DATE
                  AND dr.id IS NULL
            """, (current_user.id,))
            debug_count_no_request = cur.fetchone()['count']
            print(f"[DEBUG] long_term_items: Found {debug_count_no_request} items with no disposal request (should match notification badge)", flush=True)
            
            # 仕入れ日から3ヶ月以上経過した未売却商品を取得（PostgreSQLでは日付を直接計算）
            # 申請未作成の商品を優先表示し、申請済み（完了以外）の商品も表示
            cur.execute("""
                SELECT m.*, 
                       dr.id as disposal_request_id, dr.disposal_type, dr.status as disposal_status,
                       EXTRACT(DAY FROM (CURRENT_DATE - m.purchase_date))::INTEGER as days_since_purchase
                FROM merchandise m
                LEFT JOIN item_disposal_requests dr ON m.id = dr.merchandise_id 
                    AND dr.status != 'completed' 
                    AND dr.reason = 'long_term'
                WHERE m.user_id = %s 
                  AND m.sale_date IS NULL
                  AND m.purchase_date IS NOT NULL
                  AND m.purchase_date <= (CURRENT_DATE - INTERVAL '90 days')::DATE
                ORDER BY 
                    CASE WHEN dr.id IS NULL THEN 0 ELSE 1 END,
                    m.purchase_date ASC
            """, (current_user.id,))
            items = [dict(row) for row in cur.fetchall()]
            print(f"[DEBUG] long_term_items: Retrieved {len(items)} items after JOIN", flush=True)
        else:
            cur = conn.cursor()
            cur.row_factory = sqlite3.Row
            
            # 3ヶ月前の日付を計算（SQLite用）
            three_months_ago = datetime.now() - timedelta(days=90)
            three_months_ago_str = three_months_ago.strftime('%Y-%m-%d')
            print(f"[DEBUG] long_term_items: SQLite - three_months_ago_str={three_months_ago_str}", flush=True)
            
            # 仕入れ日から3ヶ月以上経過した未売却商品を取得
            cur.execute("""
                SELECT m.*, 
                       dr.id as disposal_request_id, dr.disposal_type, dr.status as disposal_status,
                       CAST((julianday('now') - julianday(m.purchase_date)) AS INTEGER) as days_since_purchase
                FROM merchandise m
                LEFT JOIN item_disposal_requests dr ON m.id = dr.merchandise_id AND dr.status != 'completed' AND dr.reason = 'long_term'
                WHERE m.user_id = ? 
                  AND m.sale_date IS NULL
                  AND m.purchase_date IS NOT NULL
                  AND m.purchase_date <= ?
                ORDER BY m.purchase_date ASC
            """, (current_user.id, three_months_ago_str))
            items = [dict(row) for row in cur.fetchall()]
            print(f"[DEBUG] long_term_items: SQLite - Retrieved {len(items)} items", flush=True)
        
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] long_term_items: {e}", flush=True)
        import traceback
        traceback.print_exc()
    
    # 各商品の経過日数を計算
    for item in items:
        if item.get('purchase_date'):
            try:
                purchase_date = item['purchase_date']
                # 文字列の場合はdatetimeに変換
                if isinstance(purchase_date, str):
                    purchase_date = datetime.strptime(purchase_date[:10], '%Y-%m-%d').date()
                # datetime型の場合はdate部分を取得
                elif hasattr(purchase_date, 'date') and callable(purchase_date.date):
                    purchase_date = purchase_date.date()
                # date型はそのまま使用
                
                # 経過日数を計算
                today = datetime.now().date()
                days = (today - purchase_date).days
                item['days_since_purchase'] = days
                item['months_since_purchase'] = days // 30
            except Exception as e:
                print(f"[ERROR] Date calculation error for item {item.get('id')}: {e}", flush=True)
                item['days_since_purchase'] = 0
                item['months_since_purchase'] = 0
    
    # デバッグ情報を取得
    debug_info = {}
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # 日付条件に合う商品数
            cur.execute("""
                SELECT COUNT(*) as count
                FROM merchandise m
                WHERE m.user_id = %s 
                  AND m.sale_date IS NULL
                  AND m.purchase_date IS NOT NULL
                  AND m.purchase_date <= (CURRENT_DATE - INTERVAL '90 days')::DATE
            """, (current_user.id,))
            debug_info['total_matching_date'] = cur.fetchone()['count']
            
            # 申請未作成の商品数
            cur.execute("""
                SELECT COUNT(*) as count
                FROM merchandise m
                LEFT JOIN item_disposal_requests dr ON m.id = dr.merchandise_id AND dr.status != 'completed' AND dr.reason = 'long_term'
                WHERE m.user_id = %s 
                  AND m.sale_date IS NULL
                  AND m.purchase_date IS NOT NULL
                  AND m.purchase_date <= (CURRENT_DATE - INTERVAL '90 days')::DATE
                  AND dr.id IS NULL
            """, (current_user.id,))
            debug_info['no_request'] = cur.fetchone()['count']
            
            # 現在の日付と90日前の日付
            cur.execute("SELECT CURRENT_DATE as today, (CURRENT_DATE - INTERVAL '90 days')::DATE as cutoff_date")
            date_info = cur.fetchone()
            debug_info['today'] = str(date_info['today'])
            debug_info['cutoff_date'] = str(date_info['cutoff_date'])
            
            cur.close()
            conn.close()
        else:
            three_months_ago = datetime.now() - timedelta(days=90)
            debug_info['today'] = datetime.now().strftime('%Y-%m-%d')
            debug_info['cutoff_date'] = three_months_ago.strftime('%Y-%m-%d')
    except Exception as e:
        debug_info['error'] = str(e)
    
    return render_template('long_term_items.html', items=items, debug_info=debug_info)


@app.route('/long-term-disposal-request', methods=['POST'])
@login_required
def submit_long_term_disposal_request():
    """長期在庫商品の処分申請を送信"""
    disposal_type = request.form.get('disposal_type')
    merchandise_ids = request.form.getlist('merchandise_ids')
    shipping_address = request.form.get('shipping_address', '')
    shipping_name = request.form.get('shipping_name', '')
    shipping_phone = request.form.get('shipping_phone', '')
    
    if not disposal_type or not merchandise_ids:
        flash('処分方法と商品を選択してください', 'error')
        return redirect(url_for('long_term_items'))
    
    if disposal_type not in ['auction', 'liquidation', 'shipping']:
        flash('無効な処分方法です', 'error')
        return redirect(url_for('long_term_items'))
    
    # 郵送の場合は住所必須
    if disposal_type == 'shipping' and not shipping_address:
        flash('郵送先住所を入力してください', 'error')
        return redirect(url_for('long_term_items'))
    
    conn = get_db()
    cur = conn.cursor()
    
    try:
        for merchandise_id in merchandise_ids:
            # 商品がユーザーのものか確認
            if DATABASE_URL:
                cur.execute("SELECT id FROM merchandise WHERE id = %s AND user_id = %s", 
                           (merchandise_id, current_user.id))
            else:
                cur.execute("SELECT id FROM merchandise WHERE id = ? AND user_id = ?", 
                           (merchandise_id, current_user.id))
            
            if cur.fetchone():
                # 既存の申請を確認
                if DATABASE_URL:
                    cur.execute("""
                        SELECT id FROM item_disposal_requests 
                        WHERE merchandise_id = %s AND status = 'pending' AND reason = 'long_term'
                    """, (merchandise_id,))
                else:
                    cur.execute("""
                        SELECT id FROM item_disposal_requests 
                        WHERE merchandise_id = ? AND status = 'pending' AND reason = 'long_term'
                    """, (merchandise_id,))
                
                existing = cur.fetchone()
                
                if existing:
                    # 既存の申請を更新
                    if DATABASE_URL:
                        cur.execute("""
                            UPDATE item_disposal_requests 
                            SET disposal_type = %s, shipping_address = %s, 
                                shipping_name = %s, shipping_phone = %s
                            WHERE id = %s
                        """, (disposal_type, shipping_address, shipping_name, shipping_phone, existing[0]))
                    else:
                        cur.execute("""
                            UPDATE item_disposal_requests 
                            SET disposal_type = ?, shipping_address = ?, 
                                shipping_name = ?, shipping_phone = ?
                            WHERE id = ?
                        """, (disposal_type, shipping_address, shipping_name, shipping_phone, existing[0]))
                else:
                    # 新規申請を作成（reason='long_term'）
                    if DATABASE_URL:
                        cur.execute("""
                            INSERT INTO item_disposal_requests 
                            (user_id, merchandise_id, disposal_type, reason, shipping_address, shipping_name, shipping_phone)
                            VALUES (%s, %s, %s, 'long_term', %s, %s, %s)
                        """, (current_user.id, merchandise_id, disposal_type, 
                              shipping_address, shipping_name, shipping_phone))
                    else:
                        cur.execute("""
                            INSERT INTO item_disposal_requests 
                            (user_id, merchandise_id, disposal_type, reason, shipping_address, shipping_name, shipping_phone)
                            VALUES (?, ?, ?, 'long_term', ?, ?, ?)
                        """, (current_user.id, merchandise_id, disposal_type, 
                              shipping_address, shipping_name, shipping_phone))
        
        conn.commit()
        
        disposal_type_names = {
            'auction': 'オークション販売',
            'liquidation': '在庫処分',
            'shipping': '受け取り郵送'
        }
        flash(f'{len(merchandise_ids)}件の商品を「{disposal_type_names.get(disposal_type)}」で申請しました', 'success')
    except Exception as e:
        flash(f'申請エラー: {str(e)}', 'error')
    finally:
        cur.close()
        conn.close()
    
    return redirect(url_for('long_term_items'))


def get_long_term_item_count():
    """仕入れ日から3ヶ月以上経過した未処理の長期在庫商品数を取得"""
    if not current_user.is_authenticated:
        return 0
    
    try:
        conn = get_db()
        
        if DATABASE_URL:
            cur = conn.cursor()
            # 仕入れ日から3ヶ月以上経過した未処理の長期在庫商品数を取得（PostgreSQLでは日付を直接計算）
            cur.execute("""
                SELECT COUNT(*) FROM merchandise m
                LEFT JOIN item_disposal_requests dr ON m.id = dr.merchandise_id AND dr.status != 'completed' AND dr.reason = 'long_term'
                WHERE m.user_id = %s 
                  AND m.sale_date IS NULL
                  AND m.purchase_date IS NOT NULL
                  AND m.purchase_date <= (CURRENT_DATE - INTERVAL '90 days')::DATE
                  AND dr.id IS NULL
            """, (current_user.id,))
        else:
            cur = conn.cursor()
            # 3ヶ月前の日付を計算（SQLite用）
            three_months_ago = datetime.now() - timedelta(days=90)
            three_months_ago_str = three_months_ago.strftime('%Y-%m-%d')
            
            cur.execute("""
                SELECT COUNT(*) FROM merchandise m
                LEFT JOIN item_disposal_requests dr ON m.id = dr.merchandise_id AND dr.status != 'completed' AND dr.reason = 'long_term'
                WHERE m.user_id = ? 
                  AND m.sale_date IS NULL
                  AND m.purchase_date IS NOT NULL
                  AND m.purchase_date <= ?
                  AND dr.id IS NULL
            """, (current_user.id, three_months_ago_str))
        
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    except:
        return 0


@app.context_processor
def inject_long_term_item_count():
    return dict(get_long_term_item_count=get_long_term_item_count)


@app.route('/admin/disposal-requests')
@login_required
@admin_required
def admin_disposal_requests():
    """管理者向け商品処分申請一覧"""
    conn = get_db()
    
    if DATABASE_URL:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT dr.*, m.product_name, m.brand_name, m.photo_path, m.purchase_price,
                   u.display_name, u.username, u.email
            FROM item_disposal_requests dr
            JOIN merchandise m ON dr.merchandise_id = m.id
            JOIN users u ON dr.user_id = u.id
            ORDER BY dr.created_at DESC
        """)
        requests = [dict(row) for row in cur.fetchall()]
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT dr.*, m.product_name, m.brand_name, m.photo_path, m.purchase_price,
                   u.display_name, u.username, u.email
            FROM item_disposal_requests dr
            JOIN merchandise m ON dr.merchandise_id = m.id
            JOIN users u ON dr.user_id = u.id
            ORDER BY dr.created_at DESC
        """)
        columns = [desc[0] for desc in cur.description]
        requests = [dict(zip(columns, row)) for row in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    # 統計
    pending_count = len([r for r in requests if r['status'] == 'pending'])
    processing_count = len([r for r in requests if r['status'] == 'processing'])
    completed_count = len([r for r in requests if r['status'] == 'completed'])
    
    return render_template('admin/disposal_requests.html',
                           requests=requests,
                           pending_count=pending_count,
                           processing_count=processing_count,
                           completed_count=completed_count)


@app.route('/admin/disposal-request/<int:request_id>/process', methods=['POST'])
@login_required
@admin_required
def process_disposal_request(request_id):
    """商品処分申請を処理"""
    action = request.form.get('action')  # processing, completed, rejected
    admin_note = request.form.get('admin_note', '')
    
    conn = get_db()
    cur = conn.cursor()
    
    if DATABASE_URL:
        cur.execute("""
            UPDATE item_disposal_requests 
            SET status = %s, admin_note = %s, processed_at = CURRENT_TIMESTAMP, processed_by = %s
            WHERE id = %s
        """, (action, admin_note, current_user.id, request_id))
    else:
        cur.execute("""
            UPDATE item_disposal_requests 
            SET status = ?, admin_note = ?, processed_at = CURRENT_TIMESTAMP, processed_by = ?
            WHERE id = ?
        """, (action, admin_note, current_user.id, request_id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    action_names = {
        'processing': '処理中に変更',
        'completed': '完了',
        'rejected': '却下'
    }
    flash(f'申請を{action_names.get(action, action)}しました', 'success')
    return redirect(url_for('admin_disposal_requests'))


# ===================
# Stripe決済連携
# ===================

@app.route('/admin/stripe')
@login_required
@admin_required
def admin_stripe_dashboard():
    """Stripe決済管理ダッシュボード"""
    conn = get_db()
    
    if DATABASE_URL:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT u.id, u.username, u.display_name, u.email, u.role,
                   u.stripe_customer_id, u.stripe_subscription_id, 
                   u.subscription_status, u.last_payment_date, u.next_payment_date,
                   u.overdue_since,
                   COUNT(CASE WHEN DATE_TRUNC('month', m.created_at) = DATE_TRUNC('month', CURRENT_DATE) THEN m.id END) as item_count,
                   COUNT(m.id) as total_item_count
            FROM users u
            LEFT JOIN merchandise m ON u.id = m.user_id
            WHERE u.role != 'owner'
            GROUP BY u.id, u.overdue_since
            ORDER BY u.display_name
        """)
        users = cur.fetchall()
        users = [dict(row) for row in users]
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.id, u.username, u.display_name, u.email, u.role,
                   u.stripe_customer_id, u.stripe_subscription_id, 
                   u.subscription_status, u.last_payment_date, u.next_payment_date,
                   u.overdue_since,
                   COUNT(CASE WHEN strftime('%%Y-%%m', m.created_at) = strftime('%%Y-%%m', 'now') THEN m.id END) as item_count,
                   COUNT(m.id) as total_item_count
            FROM users u
            LEFT JOIN merchandise m ON u.id = m.user_id
            WHERE u.role != 'owner'
            GROUP BY u.id
            ORDER BY u.display_name
        """)
        users = [dict(row) for row in cur.fetchall()]
    
    # 月額利用料を計算（当月の登録件数ベース）+ 未払い期間を計算
    for user in users:
        item_count = user.get('item_count', 0) or 0
        user['monthly_fee'] = get_monthly_fee(item_count)
        
        # 未払い期間を計算
        if user.get('overdue_since'):
            overdue_since = user['overdue_since']
            if isinstance(overdue_since, str):
                from datetime import datetime
                try:
                    overdue_since = datetime.strptime(overdue_since, '%Y-%m-%d %H:%M:%S')
                except:
                    try:
                        overdue_since = datetime.strptime(overdue_since, '%Y-%m-%d %H:%M:%S.%f')
                    except:
                        overdue_since = None
            
            if overdue_since:
                days_overdue = (datetime.now() - overdue_since).days
                user['overdue_days'] = days_overdue
                user['overdue_months'] = days_overdue // 30
            else:
                user['overdue_days'] = 0
                user['overdue_months'] = 0
        else:
            user['overdue_days'] = 0
            user['overdue_months'] = 0
    
    cur.close()
    conn.close()
    
    # スケジューラー情報を取得
    scheduler_info = {
        'enabled': SCHEDULER_ENABLED,
        'running': scheduler is not None and scheduler.running if SCHEDULER_ENABLED else False,
        'next_run': None
    }
    
    if SCHEDULER_ENABLED and scheduler is not None and scheduler.running:
        try:
            job = scheduler.get_job('monthly_subscription_update')
            if job and job.next_run_time:
                scheduler_info['next_run'] = job.next_run_time.strftime('%Y-%m-%d %H:%M')
        except:
            pass
    
    return render_template('admin/stripe_dashboard.html', 
                           users=users,
                           stripe_enabled=STRIPE_ENABLED and bool(STRIPE_SECRET_KEY),
                           stripe_publishable_key=STRIPE_PUBLISHABLE_KEY,
                           scheduler_info=scheduler_info)

def get_monthly_fee(item_count):
    """当月の商品登録数から月額利用料を計算
    20点→2,500円、50点→5,000円、100点→10,000円、200点→20,000円、300点→30,000円、それ以降は要相談
    """
    if item_count <= 20:
        return 2500
    elif item_count <= 50:
        return 5000
    elif item_count <= 100:
        return 10000
    elif item_count <= 200:
        return 20000
    else:
        return 30000  # 300件超は要相談

def get_or_create_stripe_price(monthly_fee):
    """Stripeの料金プラン（Price）を取得または動的に作成"""
    # 環境変数に設定されたPrice IDを使用
    price_id = STRIPE_PRICE_IDS.get(monthly_fee)
    if price_id:
        return price_id
    
    # Price IDが設定されていない場合は動的に作成
    try:
        # 既存のProductを検索または作成
        products = stripe.Product.list(limit=1, active=True)
        product_id = None
        
        for product in products.data:
            if product.metadata.get('type') == 'monthly_subscription':
                product_id = product.id
                break
        
        if not product_id:
            product = stripe.Product.create(
                name='月額利用料',
                description='商品管理システム月額利用料',
                metadata={'type': 'monthly_subscription'}
            )
            product_id = product.id
        
        # 該当金額のPriceを検索
        prices = stripe.Price.list(product=product_id, active=True)
        for price in prices.data:
            if price.unit_amount == monthly_fee and price.recurring and price.recurring.interval == 'month':
                return price.id
        
        # 見つからない場合は新規作成
        price = stripe.Price.create(
            product=product_id,
            unit_amount=monthly_fee,
            currency='jpy',
            recurring={'interval': 'month'},
            metadata={'monthly_fee': str(monthly_fee)}
        )
        return price.id
        
    except Exception as e:
        print(f"Price creation error: {e}")
        return None

@app.route('/admin/stripe/subscribe/<int:user_id>')
@login_required
@admin_required
def admin_stripe_subscribe(user_id):
    """サブスクリプション開始（自動引き落とし設定）"""
    if not STRIPE_ENABLED or not STRIPE_SECRET_KEY:
        return jsonify({'success': False, 'error': 'Stripe連携が設定されていません'})
    
    conn = get_db()
    
    if DATABASE_URL:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT u.*, 
                   COUNT(CASE WHEN DATE_TRUNC('month', m.created_at) = DATE_TRUNC('month', CURRENT_DATE) THEN m.id END) as item_count
            FROM users u
            LEFT JOIN merchandise m ON u.id = m.user_id
            WHERE u.id = %s
            GROUP BY u.id
        """, (user_id,))
        user = cur.fetchone()
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.*, 
                   COUNT(CASE WHEN strftime('%%Y-%%m', m.created_at) = strftime('%%Y-%%m', 'now') THEN m.id END) as item_count
            FROM users u
            LEFT JOIN merchandise m ON u.id = m.user_id
            WHERE u.id = ?
            GROUP BY u.id
        """, (user_id,))
        user = cur.fetchone()
        user = dict(user) if user else None
    
    if not user:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'error': 'ユーザーが見つかりません'})
    
    # 既にサブスクリプションがある場合
    if user.get('stripe_subscription_id'):
        cur.close()
        conn.close()
        return jsonify({'success': False, 'error': '既にサブスクリプションが登録されています'})
    
    # 月額利用料を計算
    item_count = user.get('item_count', 0) or 0
    monthly_fee = get_monthly_fee(item_count)
    
    try:
        # Stripe顧客を作成または取得
        stripe_customer_id = user.get('stripe_customer_id')
        if not stripe_customer_id:
            customer = stripe.Customer.create(
                email=user['email'],
                name=user.get('display_name') or user['username'],
                metadata={'user_id': str(user_id)}
            )
            stripe_customer_id = customer.id
            
            if DATABASE_URL:
                cur.execute("UPDATE users SET stripe_customer_id = %s WHERE id = %s", 
                           (stripe_customer_id, user_id))
            else:
                cur.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", 
                           (stripe_customer_id, user_id))
            conn.commit()
        
        # 料金プランを取得
        price_id = get_or_create_stripe_price(monthly_fee)
        if not price_id:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': '料金プランの作成に失敗しました'})
        
        # Checkout Session作成（サブスクリプションモード）
        # 30日間の無料トライアル付き
        base_url = request.host_url.rstrip('/')
        session = stripe.checkout.Session.create(
            customer=stripe_customer_id,
            payment_method_types=['card'],
            line_items=[{
                'price': price_id,
                'quantity': 1,
            }],
            mode='subscription',
            subscription_data={
                'trial_period_days': 30,  # 30日間無料トライアル
            },
            success_url=f'{base_url}/admin/stripe/success?session_id={{CHECKOUT_SESSION_ID}}&user_id={user_id}',
            cancel_url=f'{base_url}/admin/stripe/cancel',
            metadata={
                'user_id': str(user_id),
                'monthly_fee': str(monthly_fee)
            }
        )
        
        cur.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'checkout_url': session.url,
            'session_id': session.id
        })
        
    except Exception as e:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/stripe/change-plan/<int:user_id>')
@login_required
@admin_required
def admin_stripe_change_plan(user_id):
    """サブスクリプションのプラン変更（料金変更時）"""
    if not STRIPE_ENABLED or not STRIPE_SECRET_KEY:
        return jsonify({'success': False, 'error': 'Stripe連携が設定されていません'})
    
    conn = get_db()
    
    if DATABASE_URL:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT u.*, COUNT(m.id) as item_count
            FROM users u
            LEFT JOIN merchandise m ON u.id = m.user_id
            WHERE u.id = %s
            GROUP BY u.id
        """, (user_id,))
        user = cur.fetchone()
    else:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.*, COUNT(m.id) as item_count
            FROM users u
            LEFT JOIN merchandise m ON u.id = m.user_id
            WHERE u.id = ?
            GROUP BY u.id
        """, (user_id,))
        user = cur.fetchone()
        user = dict(user) if user else None
    
    if not user:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'error': 'ユーザーが見つかりません'})
    
    subscription_id = user.get('stripe_subscription_id')
    if not subscription_id:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'error': 'サブスクリプションが登録されていません。先にサブスクリプションを開始してください。'})
    
    # 新しい月額利用料を計算
    item_count = user.get('item_count', 0) or 0
    new_monthly_fee = get_monthly_fee(item_count)
    
    try:
        # 現在のサブスクリプションを取得
        subscription = stripe.Subscription.retrieve(subscription_id)
        current_price_id = subscription['items']['data'][0]['price']['id']
        current_price = stripe.Price.retrieve(current_price_id)
        current_fee = current_price.unit_amount
        
        # 料金が同じ場合は変更不要
        if current_fee == new_monthly_fee:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': '料金に変更はありません'})
        
        # 新しい料金プランを取得
        new_price_id = get_or_create_stripe_price(new_monthly_fee)
        if not new_price_id:
            cur.close()
            conn.close()
            return jsonify({'success': False, 'error': '新しい料金プランの作成に失敗しました'})
        
        # サブスクリプションを更新
        stripe.Subscription.modify(
            subscription_id,
            items=[{
                'id': subscription['items']['data'][0]['id'],
                'price': new_price_id,
            }],
            proration_behavior='create_prorations',  # 日割り計算を有効化
            metadata={
                'previous_fee': str(current_fee),
                'new_fee': str(new_monthly_fee),
                'changed_at': datetime.now().isoformat()
            }
        )
        
        cur.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': f'プランを ¥{current_fee:,} → ¥{new_monthly_fee:,} に変更しました',
            'previous_fee': current_fee,
            'new_fee': new_monthly_fee
        })
        
    except stripe.error.InvalidRequestError as e:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'error': f'Stripeエラー: {str(e)}'})
    except Exception as e:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/stripe/cancel-subscription/<int:user_id>')
@login_required
@admin_required
def admin_stripe_cancel_subscription(user_id):
    """サブスクリプションをキャンセル"""
    if not STRIPE_ENABLED or not STRIPE_SECRET_KEY:
        return jsonify({'success': False, 'error': 'Stripe連携が設定されていません'})
    
    conn = get_db()
    
    if DATABASE_URL:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
    else:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        user = cur.fetchone()
        user = dict(user) if user else None
    
    if not user:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'error': 'ユーザーが見つかりません'})
    
    subscription_id = user.get('stripe_subscription_id')
    if not subscription_id:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'error': 'サブスクリプションが登録されていません'})
    
    try:
        # サブスクリプションをキャンセル（期間終了時に停止）
        stripe.Subscription.modify(
            subscription_id,
            cancel_at_period_end=True
        )
        
        # DBを更新
        if DATABASE_URL:
            cur.execute("""
                UPDATE users SET subscription_status = 'canceling' WHERE id = %s
            """, (user_id,))
        else:
            cur.execute("""
                UPDATE users SET subscription_status = 'canceling' WHERE id = ?
            """, (user_id,))
        
        conn.commit()
        cur.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': 'サブスクリプションは現在の期間終了時にキャンセルされます'
        })
        
    except Exception as e:
        cur.close()
        conn.close()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/stripe/success')
@login_required
@admin_required
def admin_stripe_success():
    """サブスクリプション登録成功"""
    session_id = request.args.get('session_id')
    user_id = request.args.get('user_id')
    
    if session_id and user_id and STRIPE_ENABLED:
        try:
            session = stripe.checkout.Session.retrieve(session_id)
            subscription_id = session.get('subscription')
            
            if subscription_id:
                conn = get_db()
                cur = conn.cursor()
                
                # サブスクリプション情報を取得
                subscription = stripe.Subscription.retrieve(subscription_id)
                current_period_end = datetime.fromtimestamp(subscription.current_period_end)
                
                now = datetime.now()
                if DATABASE_URL:
                    cur.execute("""
                        UPDATE users 
                        SET stripe_subscription_id = %s,
                            subscription_status = 'active',
                            last_payment_date = %s,
                            next_payment_date = %s
                        WHERE id = %s
                    """, (subscription_id, now, current_period_end, user_id))
                else:
                    cur.execute("""
                        UPDATE users 
                        SET stripe_subscription_id = ?,
                            subscription_status = 'active',
                            last_payment_date = ?,
                            next_payment_date = ?
                        WHERE id = ?
                    """, (subscription_id, now, current_period_end, user_id))
                
                conn.commit()
                cur.close()
                conn.close()
                
                flash('サブスクリプション登録が完了しました。毎月自動で引き落としされます。', 'success')
        except Exception as e:
            flash(f'登録確認エラー: {str(e)}', 'error')
    
    return redirect(url_for('admin_stripe_dashboard'))

@app.route('/admin/stripe/cancel')
@login_required
@admin_required
def admin_stripe_cancel():
    """支払いキャンセル"""
    flash('登録がキャンセルされました', 'info')
    return redirect(url_for('admin_stripe_dashboard'))

@app.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Stripe Webhookエンドポイント"""
    if not STRIPE_ENABLED:
        return jsonify({'error': 'Stripe not enabled'}), 400
    
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        else:
            event = json.loads(payload)
    except ValueError as e:
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError as e:
        return jsonify({'error': 'Invalid signature'}), 400
    
    # イベント処理
    event_type = event['type']
    
    # サブスクリプション作成完了
    if event_type == 'customer.subscription.created':
        subscription = event['data']['object']
        customer_id = subscription['customer']
        subscription_id = subscription['id']
        
        conn = get_db()
        cur = conn.cursor()
        
        current_period_end = datetime.fromtimestamp(subscription['current_period_end'])
        now = datetime.now()
        
        if DATABASE_URL:
            cur.execute("""
                UPDATE users 
                SET stripe_subscription_id = %s,
                    subscription_status = 'active',
                    last_payment_date = %s,
                    next_payment_date = %s
                WHERE stripe_customer_id = %s
            """, (subscription_id, now, current_period_end, customer_id))
        else:
            cur.execute("""
                UPDATE users 
                SET stripe_subscription_id = ?,
                    subscription_status = 'active',
                    last_payment_date = ?,
                    next_payment_date = ?
                WHERE stripe_customer_id = ?
            """, (subscription_id, now, current_period_end, customer_id))
        
        conn.commit()
        cur.close()
        conn.close()
    
    # サブスクリプション更新（毎月の支払い成功）
    elif event_type == 'invoice.payment_succeeded':
        invoice = event['data']['object']
        subscription_id = invoice.get('subscription')
        
        if subscription_id:
            conn = get_db()
            cur = conn.cursor()
            
            # サブスクリプション情報を取得して次回支払日を更新
            try:
                subscription = stripe.Subscription.retrieve(subscription_id)
                current_period_end = datetime.fromtimestamp(subscription['current_period_end'])
                now = datetime.now()
                
                if DATABASE_URL:
                    cur.execute("""
                        UPDATE users 
                        SET subscription_status = 'active',
                            last_payment_date = %s,
                            next_payment_date = %s
                        WHERE stripe_subscription_id = %s
                    """, (now, current_period_end, subscription_id))
                else:
                    cur.execute("""
                        UPDATE users 
                        SET subscription_status = 'active',
                            last_payment_date = ?,
                            next_payment_date = ?
                        WHERE stripe_subscription_id = ?
                    """, (now, current_period_end, subscription_id))
                
                conn.commit()
            except Exception as e:
                print(f"Webhook invoice.payment_succeeded error: {e}")
            finally:
                cur.close()
                conn.close()
    
    # 支払い失敗
    elif event_type == 'invoice.payment_failed':
        invoice = event['data']['object']
        subscription_id = invoice.get('subscription')
        
        if subscription_id:
            conn = get_db()
            cur = conn.cursor(cursor_factory=RealDictCursor) if DATABASE_URL else conn.cursor()
            
            # ユーザー情報を取得
            if DATABASE_URL:
                cur.execute("""
                    SELECT id, display_name, username, line_user_id, email, overdue_since 
                    FROM users WHERE stripe_subscription_id = %s
                """, (subscription_id,))
            else:
                cur.execute("""
                    SELECT id, display_name, username, line_user_id, email, overdue_since 
                    FROM users WHERE stripe_subscription_id = ?
                """, (subscription_id,))
            
            user = cur.fetchone()
            
            if user:
                user_dict = dict(user) if DATABASE_URL else {
                    'id': user[0], 'display_name': user[1], 'username': user[2],
                    'line_user_id': user[3], 'email': user[4], 'overdue_since': user[5]
                }
                
                # 未払い開始日を記録（初回のみ）
                if DATABASE_URL:
                    cur.execute("""
                        UPDATE users 
                        SET subscription_status = 'past_due',
                            overdue_since = COALESCE(overdue_since, CURRENT_TIMESTAMP)
                        WHERE stripe_subscription_id = %s
                    """, (subscription_id,))
                else:
                    cur.execute("""
                        UPDATE users 
                        SET subscription_status = 'past_due',
                            overdue_since = COALESCE(overdue_since, CURRENT_TIMESTAMP)
                        WHERE stripe_subscription_id = ?
                    """, (subscription_id,))
                
                # LINE通知送信
                line_user_id = user_dict.get('line_user_id')
                display_name = user_dict.get('display_name') or user_dict.get('username') or 'ユーザー'
                
                if line_user_id:
                    message = f"""⚠️ 月謝のお支払いが確認できませんでした

{display_name}様

今月の月謝のお支払いが確認できませんでした。
お早めにお支払いをお願いいたします。

━━━━━━━━━━━━━━━━━
【重要なお知らせ】
━━━━━━━━━━━━━━━━━
・未払い期間中は画面の閲覧と商品処分のみ可能です
・商品の追加・編集はできなくなります
・3ヶ月以上未払いが続くと、ログインができなくなり商品は自動的に管理者へ移管されます

ご不明点がございましたらお問い合わせください。"""
                    try:
                        send_line_push(line_user_id, message)
                    except Exception as e:
                        print(f"LINE notification error: {e}")
            else:
                # ユーザーが見つからない場合は従来の処理
                if DATABASE_URL:
                    cur.execute("""
                        UPDATE users SET subscription_status = 'past_due'
                        WHERE stripe_subscription_id = %s
                    """, (subscription_id,))
                else:
                    cur.execute("""
                        UPDATE users SET subscription_status = 'past_due'
                        WHERE stripe_subscription_id = ?
                    """, (subscription_id,))
            
            conn.commit()
            cur.close()
            conn.close()
    
    # サブスクリプションキャンセル
    elif event_type == 'customer.subscription.deleted':
        subscription = event['data']['object']
        subscription_id = subscription['id']
        
        conn = get_db()
        cur = conn.cursor()
        
        if DATABASE_URL:
            cur.execute("""
                UPDATE users 
                SET stripe_subscription_id = NULL,
                    subscription_status = 'canceled'
                WHERE stripe_subscription_id = %s
            """, (subscription_id,))
        else:
            cur.execute("""
                UPDATE users 
                SET stripe_subscription_id = NULL,
                    subscription_status = 'canceled'
                WHERE stripe_subscription_id = ?
            """, (subscription_id,))
        
        conn.commit()
        cur.close()
        conn.close()
    
    # サブスクリプション更新（プラン変更など）
    elif event_type == 'customer.subscription.updated':
        subscription = event['data']['object']
        subscription_id = subscription['id']
        status = subscription['status']
        
        conn = get_db()
        cur = conn.cursor()
        
        current_period_end = datetime.fromtimestamp(subscription['current_period_end'])
        
        # Stripeのステータスをマッピング
        status_map = {
            'active': 'active',
            'past_due': 'past_due',
            'canceled': 'canceled',
            'unpaid': 'unpaid',
            'trialing': 'active'
        }
        mapped_status = status_map.get(status, status)
        
        if DATABASE_URL:
            cur.execute("""
                UPDATE users 
                SET subscription_status = %s,
                    next_payment_date = %s
                WHERE stripe_subscription_id = %s
            """, (mapped_status, current_period_end, subscription_id))
        else:
            cur.execute("""
                UPDATE users 
                SET subscription_status = ?,
                    next_payment_date = ?
                WHERE stripe_subscription_id = ?
            """, (mapped_status, current_period_end, subscription_id))
        
        conn.commit()
        cur.close()
        conn.close()
    
    return jsonify({'received': True})

# ===================
# 月末バッチ処理（料金自動更新）
# ===================

@app.route('/admin/stripe/batch-update', methods=['POST'])
@login_required
@admin_required
def admin_stripe_batch_update():
    """月末バッチ処理：全ユーザーの料金プランを商品数に基づいて自動更新"""
    if not STRIPE_ENABLED or not STRIPE_SECRET_KEY:
        return jsonify({'success': False, 'error': 'Stripe連携が設定されていません'})
    
    conn = get_db()
    results = {
        'processed': 0,
        'updated': 0,
        'unchanged': 0,
        'errors': 0,
        'details': []
    }
    
    try:
        if DATABASE_URL:
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # サブスク登録済みユーザーを取得
            cur.execute("""
                SELECT u.id, u.username, u.display_name, u.email,
                       u.stripe_customer_id, u.stripe_subscription_id,
                       COUNT(m.id) as item_count
                FROM users u
                LEFT JOIN merchandise m ON u.id = m.user_id
                WHERE u.stripe_subscription_id IS NOT NULL
                  AND u.subscription_status = 'active'
                GROUP BY u.id
            """)
            users = cur.fetchall()
            users = [dict(row) for row in users]
        else:
            cur = conn.cursor()
            cur.execute("""
                SELECT u.id, u.username, u.display_name, u.email,
                       u.stripe_customer_id, u.stripe_subscription_id,
                       COUNT(m.id) as item_count
                FROM users u
                LEFT JOIN merchandise m ON u.id = m.user_id
                WHERE u.stripe_subscription_id IS NOT NULL
                  AND u.subscription_status = 'active'
                GROUP BY u.id
            """)
            users = [dict(row) for row in cur.fetchall()]
        
        for user in users:
            results['processed'] += 1
            user_name = user.get('display_name') or user.get('username')
            subscription_id = user.get('stripe_subscription_id')
            
            try:
                # 現在の商品数から新しい月額を計算
                item_count = user.get('item_count', 0) or 0
                new_monthly_fee = get_monthly_fee(item_count)
                
                # 現在のサブスクリプション情報を取得
                subscription = stripe.Subscription.retrieve(subscription_id)
                current_price_id = subscription['items']['data'][0]['price']['id']
                current_price = stripe.Price.retrieve(current_price_id)
                current_fee = current_price.unit_amount
                
                if current_fee == new_monthly_fee:
                    # 料金変更なし
                    results['unchanged'] += 1
                    results['details'].append({
                        'user': user_name,
                        'status': 'unchanged',
                        'fee': current_fee,
                        'item_count': item_count
                    })
                else:
                    # 料金変更あり → プラン更新
                    new_price_id = get_or_create_stripe_price(new_monthly_fee)
                    if new_price_id:
                        stripe.Subscription.modify(
                            subscription_id,
                            items=[{
                                'id': subscription['items']['data'][0]['id'],
                                'price': new_price_id,
                            }],
                            proration_behavior='none',  # 日割り計算なし（月末締めなので）
                            metadata={
                                'previous_fee': str(current_fee),
                                'new_fee': str(new_monthly_fee),
                                'updated_at': datetime.now().isoformat(),
                                'batch_update': 'true'
                            }
                        )
                        
                        results['updated'] += 1
                        results['details'].append({
                            'user': user_name,
                            'status': 'updated',
                            'previous_fee': current_fee,
                            'new_fee': new_monthly_fee,
                            'item_count': item_count
                        })
                    else:
                        results['errors'] += 1
                        results['details'].append({
                            'user': user_name,
                            'status': 'error',
                            'error': '料金プランの作成に失敗'
                        })
                        
            except Exception as e:
                results['errors'] += 1
                results['details'].append({
                    'user': user_name,
                    'status': 'error',
                    'error': str(e)
                })
        
        cur.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'message': f"処理完了: {results['processed']}件処理, {results['updated']}件更新, {results['unchanged']}件変更なし, {results['errors']}件エラー",
            'results': results
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })

@app.route('/api/stripe/batch-update', methods=['POST'])
def api_stripe_batch_update():
    """外部からのバッチ実行用API（cronやスケジューラー用）
    
    セキュリティのため、環境変数BATCH_API_KEYを設定し、
    リクエストヘッダーにX-API-Key: <key>を含める必要があります。
    """
    api_key = os.environ.get('BATCH_API_KEY', '')
    request_key = request.headers.get('X-API-Key', '')
    
    if not api_key or api_key != request_key:
        return jsonify({'success': False, 'error': 'Invalid API key'}), 401
    
    if not STRIPE_ENABLED or not STRIPE_SECRET_KEY:
        return jsonify({'success': False, 'error': 'Stripe not configured'}), 400
    
    conn = get_db()
    results = {
        'processed': 0,
        'updated': 0,
        'unchanged': 0,
        'errors': 0,
        'timestamp': datetime.now().isoformat()
    }
    
    try:
        if DATABASE_URL:
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT u.id, u.username, u.display_name,
                       u.stripe_subscription_id,
                       COUNT(m.id) as item_count
                FROM users u
                LEFT JOIN merchandise m ON u.id = m.user_id
                WHERE u.stripe_subscription_id IS NOT NULL
                  AND u.subscription_status = 'active'
                GROUP BY u.id
            """)
            users = cur.fetchall()
            users = [dict(row) for row in users]
        else:
            cur = conn.cursor()
            cur.execute("""
                SELECT u.id, u.username, u.display_name,
                       u.stripe_subscription_id,
                       COUNT(m.id) as item_count
                FROM users u
                LEFT JOIN merchandise m ON u.id = m.user_id
                WHERE u.stripe_subscription_id IS NOT NULL
                  AND u.subscription_status = 'active'
                GROUP BY u.id
            """)
            users = [dict(row) for row in cur.fetchall()]
        
        for user in users:
            results['processed'] += 1
            subscription_id = user.get('stripe_subscription_id')
            
            try:
                item_count = user.get('item_count', 0) or 0
                new_monthly_fee = get_monthly_fee(item_count)
                
                subscription = stripe.Subscription.retrieve(subscription_id)
                current_price_id = subscription['items']['data'][0]['price']['id']
                current_price = stripe.Price.retrieve(current_price_id)
                current_fee = current_price.unit_amount
                
                if current_fee == new_monthly_fee:
                    results['unchanged'] += 1
                else:
                    new_price_id = get_or_create_stripe_price(new_monthly_fee)
                    if new_price_id:
                        stripe.Subscription.modify(
                            subscription_id,
                            items=[{
                                'id': subscription['items']['data'][0]['id'],
                                'price': new_price_id,
                            }],
                            proration_behavior='none'
                        )
                        results['updated'] += 1
                    else:
                        results['errors'] += 1
                        
            except Exception as e:
                results['errors'] += 1
        
        cur.close()
        conn.close()
        
        return jsonify({
            'success': True,
            'results': results
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# ===================
# 月末自動スケジューラー
# ===================

def run_monthly_batch_update():
    """月末バッチ処理（スケジューラーから呼び出し）"""
    if not STRIPE_ENABLED or not STRIPE_SECRET_KEY:
        print(f"[{datetime.now()}] Monthly batch skipped: Stripe not configured")
        return
    
    print(f"[{datetime.now()}] Starting monthly batch update...")
    
    with app.app_context():
        conn = get_db()
        results = {'processed': 0, 'updated': 0, 'unchanged': 0, 'errors': 0}
        
        try:
            if DATABASE_URL:
                from psycopg2.extras import RealDictCursor
                cur = conn.cursor(cursor_factory=RealDictCursor)
                cur.execute("""
                    SELECT u.id, u.username, u.display_name,
                           u.stripe_subscription_id,
                           COUNT(m.id) as item_count
                    FROM users u
                    LEFT JOIN merchandise m ON u.id = m.user_id
                    WHERE u.stripe_subscription_id IS NOT NULL
                      AND u.subscription_status = 'active'
                    GROUP BY u.id
                """)
                users = cur.fetchall()
                users = [dict(row) for row in users]
            else:
                cur = conn.cursor()
                cur.execute("""
                    SELECT u.id, u.username, u.display_name,
                           u.stripe_subscription_id,
                           COUNT(m.id) as item_count
                    FROM users u
                    LEFT JOIN merchandise m ON u.id = m.user_id
                    WHERE u.stripe_subscription_id IS NOT NULL
                      AND u.subscription_status = 'active'
                    GROUP BY u.id
                """)
                users = [dict(row) for row in cur.fetchall()]
            
            for user in users:
                results['processed'] += 1
                subscription_id = user.get('stripe_subscription_id')
                user_name = user.get('display_name') or user.get('username')
                
                try:
                    item_count = user.get('item_count', 0) or 0
                    new_monthly_fee = get_monthly_fee(item_count)
                    
                    subscription = stripe.Subscription.retrieve(subscription_id)
                    current_price_id = subscription['items']['data'][0]['price']['id']
                    current_price = stripe.Price.retrieve(current_price_id)
                    current_fee = current_price.unit_amount
                    
                    if current_fee == new_monthly_fee:
                        results['unchanged'] += 1
                    else:
                        new_price_id = get_or_create_stripe_price(new_monthly_fee)
                        if new_price_id:
                            stripe.Subscription.modify(
                                subscription_id,
                                items=[{
                                    'id': subscription['items']['data'][0]['id'],
                                    'price': new_price_id,
                                }],
                                proration_behavior='none'
                            )
                            results['updated'] += 1
                            print(f"  Updated {user_name}: ¥{current_fee:,} → ¥{new_monthly_fee:,}")
                        else:
                            results['errors'] += 1
                            
                except Exception as e:
                    results['errors'] += 1
                    print(f"  Error for {user_name}: {e}")
            
            cur.close()
            conn.close()
            
            print(f"[{datetime.now()}] Monthly batch completed: "
                  f"{results['processed']} processed, {results['updated']} updated, "
                  f"{results['unchanged']} unchanged, {results['errors']} errors")
            
        except Exception as e:
            print(f"[{datetime.now()}] Monthly batch failed: {e}")

# スケジューラー初期化
scheduler = None

def init_scheduler():
    """スケジューラーを初期化"""
    global scheduler
    
    if not SCHEDULER_ENABLED:
        print("APScheduler not installed, skipping scheduler initialization")
        return
    
    # 既にスケジューラーが動作中の場合はスキップ
    if scheduler is not None and scheduler.running:
        return
    
    scheduler = BackgroundScheduler(timezone='Asia/Tokyo')
    
    # 月末23:59に実行（毎月最終日）
    # day='last' は月の最終日を意味する
    scheduler.add_job(
        run_monthly_batch_update,
        CronTrigger(day='last', hour=23, minute=59, timezone='Asia/Tokyo'),
        id='monthly_subscription_update',
        name='月末サブスクリプション料金更新',
        replace_existing=True
    )
    
    # LINE定期送信（毎分チェック）
    scheduler.add_job(
        run_scheduled_line_messages,
        CronTrigger(minute='*', timezone='Asia/Tokyo'),
        id='line_scheduled_messages',
        name='LINE定期送信',
        replace_existing=True
    )
    
    # 未払い商品の自動移動（毎日午前3時に実行）
    scheduler.add_job(
        check_and_transfer_overdue_items,
        CronTrigger(hour=3, minute=0, timezone='Asia/Tokyo'),
        id='overdue_items_transfer',
        name='未払い商品自動移動',
        replace_existing=True
    )
    
    # 長期在庫商品の自動移動（毎日午前4時に実行）
    scheduler.add_job(
        check_and_transfer_long_term_items,
        CronTrigger(hour=4, minute=0, timezone='Asia/Tokyo'),
        id='long_term_items_transfer',
        name='長期在庫商品自動移動',
        replace_existing=True
    )
    
    scheduler.start()
    print(f"[{datetime.now()}] Scheduler started: Monthly batch will run on last day of each month at 23:59 JST")
    print(f"[{datetime.now()}] Scheduler started: LINE scheduled messages will be checked every minute")
    print(f"[{datetime.now()}] Scheduler started: Overdue items check will run daily at 03:00 JST")

# =============================================
# 買取承諾書（ユーザー向け）
# =============================================
@app.route('/kaitori-shoudaku')
@login_required
def user_kaitori_shoudaku_list():
    """買取承諾書一覧（ユーザー用）"""
    kaitori_list = []
    try:
        conn = get_db()
        
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute('''
                SELECT * FROM user_kaitori_shoudaku 
                WHERE user_id = %s 
                ORDER BY created_at DESC
            ''', (current_user.id,))
            rows = cur.fetchall()
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute('''
                SELECT * FROM user_kaitori_shoudaku 
                WHERE user_id = ? 
                ORDER BY created_at DESC
            ''', (current_user.id,))
            rows = cur.fetchall()
        
        for row in rows:
            item = dict(row)
            for key in ['created_at', 'updated_at', 'issue_date']:
                if item.get(key) and hasattr(item[key], 'strftime'):
                    item[key] = item[key].strftime('%Y-%m-%d %H:%M:%S') if key.endswith('_at') else item[key].strftime('%Y-%m-%d')
            kaitori_list.append(item)
        
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] user_kaitori_shoudaku_list: {e}", flush=True)
    
    return render_template('kaitori_shoudaku_list.html', kaitori_list=kaitori_list)

@app.route('/kaitori-shoudaku/add', methods=['GET', 'POST'])
@login_required
def user_kaitori_shoudaku_add():
    """買取承諾書作成（ユーザー用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()
    
    if request.method == 'POST':
        # 書類番号生成
        document_no = f"KS-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        customer_name = request.form.get('customer_name', '')
        customer_address = request.form.get('customer_address', '')
        customer_phone = request.form.get('customer_phone', '')
        issue_date = request.form.get('issue_date', datetime.now().strftime('%Y-%m-%d'))
        payment_method = request.form.get('payment_method', '')
        notes = request.form.get('notes', '')
        
        # 明細データ取得
        product_names = request.form.getlist('product_name[]')
        brand_names = request.form.getlist('brand_name[]')
        conditions = request.form.getlist('condition[]')
        quantities = request.form.getlist('quantity[]')
        unit_prices = request.form.getlist('unit_price[]')
        
        # 合計計算
        subtotal = 0
        for i in range(len(product_names)):
            if product_names[i]:
                qty_str = quantities[i] if i < len(quantities) else ''
                price_str = unit_prices[i] if i < len(unit_prices) else ''
                qty = int(qty_str) if qty_str else 1
                price = int(price_str) if price_str else 0
                subtotal += qty * price
        
        total_amount = subtotal  # 消費税なし
        
        try:
            if DATABASE_URL:
                cur.execute('''
                    INSERT INTO user_kaitori_shoudaku 
                    (document_no, user_id, customer_name, customer_address, customer_phone, 
                    issue_date, subtotal, total_amount, payment_method, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (document_no, current_user.id, customer_name, customer_address, customer_phone,
                      issue_date, subtotal, total_amount, payment_method, notes))
                kaitori_id = cur.fetchone()['id']
            else:
                cur.execute('''
                    INSERT INTO user_kaitori_shoudaku 
                    (document_no, user_id, customer_name, customer_address, customer_phone, 
                    issue_date, subtotal, total_amount, payment_method, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (document_no, current_user.id, customer_name, customer_address, customer_phone,
                      issue_date, subtotal, total_amount, payment_method, notes))
                kaitori_id = cur.lastrowid
            
            # 明細追加
            for i, product_name in enumerate(product_names):
                if product_name:
                    qty_str = quantities[i] if i < len(quantities) else ''
                    price_str = unit_prices[i] if i < len(unit_prices) else ''
                    qty = int(qty_str) if qty_str else 1
                    price = int(price_str) if price_str else 0
                    amount = qty * price
                    
                    if DATABASE_URL:
                        cur.execute('''
                            INSERT INTO user_kaitori_shoudaku_items 
                            (kaitori_shoudaku_id, item_no, product_name, brand_name, condition, quantity, unit_price, amount)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ''', (kaitori_id, i+1, product_name, brand_names[i] if i < len(brand_names) else '',
                              conditions[i] if i < len(conditions) else '', qty, price, amount))
                    else:
                        cur.execute('''
                            INSERT INTO user_kaitori_shoudaku_items 
                            (kaitori_shoudaku_id, item_no, product_name, brand_name, condition, quantity, unit_price, amount)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (kaitori_id, i+1, product_name, brand_names[i] if i < len(brand_names) else '',
                              conditions[i] if i < len(conditions) else '', qty, price, amount))
            
            conn.commit()
        except Exception as e:
            conn.rollback()
            cur.close()
            conn.close()
            flash(f'買取承諾書の作成に失敗しました: {str(e)}', 'error')
            return redirect(url_for('user_kaitori_shoudaku_add'))
        
        cur.close()
        conn.close()
        
        flash('買取承諾書を作成しました', 'success')
        return redirect(url_for('user_kaitori_shoudaku_list'))
    
    # GETリクエスト
    cur.close()
    conn.close()
    
    today = datetime.now().strftime('%Y-%m-%d')
    return render_template('kaitori_shoudaku_form.html', kaitori=None, items=[], mode='add', today=today)

@app.route('/kaitori-shoudaku/<int:id>')
@login_required
def user_kaitori_shoudaku_view(id):
    """買取承諾書詳細表示（ユーザー用）"""
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute('SELECT * FROM user_kaitori_shoudaku WHERE id = %s AND user_id = %s', (id, current_user.id))
        kaitori = cur.fetchone()
        if kaitori:
            kaitori = dict(kaitori)
            cur.execute('SELECT * FROM user_kaitori_shoudaku_items WHERE kaitori_shoudaku_id = %s ORDER BY item_no', (id,))
            items = [dict(row) for row in cur.fetchall()]
        else:
            items = []
    else:
        cur.execute('SELECT * FROM user_kaitori_shoudaku WHERE id = ? AND user_id = ?', (id, current_user.id))
        row = cur.fetchone()
        if row:
            kaitori = dict(zip([d[0] for d in cur.description], row))
            cur.execute('SELECT * FROM user_kaitori_shoudaku_items WHERE kaitori_shoudaku_id = ? ORDER BY item_no', (id,))
            items = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        else:
            kaitori = None
            items = []
    
    cur.close()
    conn.close()
    
    if not kaitori:
        flash('買取承諾書が見つかりません', 'error')
        return redirect(url_for('user_kaitori_shoudaku_list'))
    
    return render_template('kaitori_shoudaku_view.html', kaitori=kaitori, items=items)

@app.route('/kaitori-shoudaku/<int:id>/edit', methods=['GET', 'POST'])
@login_required
def user_kaitori_shoudaku_edit(id):
    """買取承諾書編集（ユーザー用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()
    
    # データ取得
    if DATABASE_URL:
        cur.execute('SELECT * FROM user_kaitori_shoudaku WHERE id = %s AND user_id = %s', (id, current_user.id))
        kaitori = cur.fetchone()
        if kaitori:
            kaitori = dict(kaitori)
    else:
        cur.execute('SELECT * FROM user_kaitori_shoudaku WHERE id = ? AND user_id = ?', (id, current_user.id))
        row = cur.fetchone()
        kaitori = dict(zip([d[0] for d in cur.description], row)) if row else None
    
    if not kaitori:
        cur.close()
        conn.close()
        flash('買取承諾書が見つかりません', 'error')
        return redirect(url_for('user_kaitori_shoudaku_list'))
    
    if request.method == 'POST':
        customer_name = request.form.get('customer_name', '')
        customer_address = request.form.get('customer_address', '')
        customer_phone = request.form.get('customer_phone', '')
        issue_date = request.form.get('issue_date', datetime.now().strftime('%Y-%m-%d'))
        payment_method = request.form.get('payment_method', '')
        notes = request.form.get('notes', '')
        
        # 明細データ取得
        product_names = request.form.getlist('product_name[]')
        brand_names = request.form.getlist('brand_name[]')
        conditions = request.form.getlist('condition[]')
        quantities = request.form.getlist('quantity[]')
        unit_prices = request.form.getlist('unit_price[]')
        
        # 合計計算
        subtotal = 0
        for i in range(len(product_names)):
            if product_names[i]:
                qty_str = quantities[i] if i < len(quantities) else ''
                price_str = unit_prices[i] if i < len(unit_prices) else ''
                qty = int(qty_str) if qty_str else 1
                price = int(price_str) if price_str else 0
                subtotal += qty * price
        
        total_amount = subtotal
        
        try:
            # 更新
            if DATABASE_URL:
                cur.execute('''
                    UPDATE user_kaitori_shoudaku 
                    SET customer_name = %s, customer_address = %s, customer_phone = %s, 
                    issue_date = %s, subtotal = %s, total_amount = %s, payment_method = %s, notes = %s,
                    updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                ''', (customer_name, customer_address, customer_phone, issue_date, subtotal, total_amount, payment_method, notes, id))
                
                # 明細削除して再作成
                cur.execute('DELETE FROM user_kaitori_shoudaku_items WHERE kaitori_shoudaku_id = %s', (id,))
            else:
                cur.execute('''
                    UPDATE user_kaitori_shoudaku 
                    SET customer_name = ?, customer_address = ?, customer_phone = ?, 
                    issue_date = ?, subtotal = ?, total_amount = ?, payment_method = ?, notes = ?,
                    updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (customer_name, customer_address, customer_phone, issue_date, subtotal, total_amount, payment_method, notes, id))
                
                cur.execute('DELETE FROM user_kaitori_shoudaku_items WHERE kaitori_shoudaku_id = ?', (id,))
            
            # 明細追加
            for i, product_name in enumerate(product_names):
                if product_name:
                    qty_str = quantities[i] if i < len(quantities) else ''
                    price_str = unit_prices[i] if i < len(unit_prices) else ''
                    qty = int(qty_str) if qty_str else 1
                    price = int(price_str) if price_str else 0
                    amount = qty * price
                    
                    if DATABASE_URL:
                        cur.execute('''
                            INSERT INTO user_kaitori_shoudaku_items 
                            (kaitori_shoudaku_id, item_no, product_name, brand_name, condition, quantity, unit_price, amount)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ''', (id, i+1, product_name, brand_names[i] if i < len(brand_names) else '',
                              conditions[i] if i < len(conditions) else '', qty, price, amount))
                    else:
                        cur.execute('''
                            INSERT INTO user_kaitori_shoudaku_items 
                            (kaitori_shoudaku_id, item_no, product_name, brand_name, condition, quantity, unit_price, amount)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (id, i+1, product_name, brand_names[i] if i < len(brand_names) else '',
                              conditions[i] if i < len(conditions) else '', qty, price, amount))
            
            conn.commit()
        except Exception as e:
            conn.rollback()
            cur.close()
            conn.close()
            flash(f'買取承諾書の更新に失敗しました: {str(e)}', 'error')
            return redirect(url_for('user_kaitori_shoudaku_edit', id=id))
        
        cur.close()
        conn.close()
        
        flash('買取承諾書を更新しました', 'success')
        return redirect(url_for('user_kaitori_shoudaku_view', id=id))
    
    # GETリクエスト - 明細取得
    if DATABASE_URL:
        cur.execute('SELECT * FROM user_kaitori_shoudaku_items WHERE kaitori_shoudaku_id = %s ORDER BY item_no', (id,))
        items = [dict(row) for row in cur.fetchall()]
    else:
        cur.execute('SELECT * FROM user_kaitori_shoudaku_items WHERE kaitori_shoudaku_id = ? ORDER BY item_no', (id,))
        items = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    
    cur.close()
    conn.close()
    
    return render_template('kaitori_shoudaku_form.html', kaitori=kaitori, items=items, mode='edit')

@app.route('/kaitori-shoudaku/<int:id>/delete')
@login_required
def user_kaitori_shoudaku_delete(id):
    """買取承諾書削除（ユーザー用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()
    
    if DATABASE_URL:
        cur.execute('DELETE FROM user_kaitori_shoudaku WHERE id = %s AND user_id = %s', (id, current_user.id))
    else:
        cur.execute('DELETE FROM user_kaitori_shoudaku WHERE id = ? AND user_id = ?', (id, current_user.id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('買取承諾書を削除しました', 'success')
    return redirect(url_for('user_kaitori_shoudaku_list'))

@app.route('/kaitori-shoudaku/<int:id>/pdf')
@login_required
def user_kaitori_shoudaku_pdf(id):
    """買取承諾書PDF出力（ユーザー用）"""
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()
    
    if DATABASE_URL:
        cur.execute('SELECT * FROM user_kaitori_shoudaku WHERE id = %s AND user_id = %s', (id, current_user.id))
        kaitori = cur.fetchone()
        if kaitori:
            kaitori = dict(kaitori)
            cur.execute('SELECT * FROM user_kaitori_shoudaku_items WHERE kaitori_shoudaku_id = %s ORDER BY item_no', (id,))
            items = [dict(row) for row in cur.fetchall()]
        else:
            items = []
    else:
        cur.execute('SELECT * FROM user_kaitori_shoudaku WHERE id = ? AND user_id = ?', (id, current_user.id))
        row = cur.fetchone()
        if row:
            kaitori = dict(zip([d[0] for d in cur.description], row))
            cur.execute('SELECT * FROM user_kaitori_shoudaku_items WHERE kaitori_shoudaku_id = ? ORDER BY item_no', (id,))
            items = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        else:
            kaitori = None
            items = []
    
    cur.close()
    conn.close()
    
    if not kaitori:
        flash('買取承諾書が見つかりません', 'error')
        return redirect(url_for('user_kaitori_shoudaku_list'))
    
    return render_template('pdf/kaitori_shoudaku_pdf.html', kaitori=kaitori, items=items)

# =============================================
# 買取承諾書（法人版・管理者用）
# =============================================
@app.route('/admin/kaitori-shoudaku')
@login_required
def admin_kaitori_shoudaku_list():
    """買取承諾書一覧（法人版・管理者用）"""
    if not current_user.is_admin():
        flash('権限がありません', 'error')
        return redirect(url_for('index'))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()
    
    if DATABASE_URL:
        cur.execute('''
            SELECT k.*, u.display_name as admin_name 
            FROM admin_kaitori_shoudaku k
            LEFT JOIN users u ON k.admin_id = u.id
            ORDER BY k.created_at DESC
        ''')
        kaitori_list = [dict(row) for row in cur.fetchall()]
    else:
        cur.execute('''
            SELECT k.*, u.display_name as admin_name 
            FROM admin_kaitori_shoudaku k
            LEFT JOIN users u ON k.admin_id = u.id
            ORDER BY k.created_at DESC
        ''')
        kaitori_list = [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
    
    cur.close()
    if DATABASE_URL:
        conn.close()
    
    return render_template('admin/kaitori_shoudaku_list.html', kaitori_list=kaitori_list)

@app.route('/admin/kaitori-shoudaku/add', methods=['GET', 'POST'])
@login_required
def admin_kaitori_shoudaku_add():
    """買取承諾書作成（法人版・管理者用）"""
    if not current_user.is_admin():
        flash('権限がありません', 'error')
        return redirect(url_for('index'))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()
    
    if request.method == 'POST':
        document_no = f"KSH-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        
        company_name = request.form.get('company_name', '')
        company_address = request.form.get('company_address', '')
        company_phone = request.form.get('company_phone', '')
        contact_name = request.form.get('contact_name', '')
        issue_date = request.form.get('issue_date', datetime.now().strftime('%Y-%m-%d'))
        payment_method = request.form.get('payment_method', '')
        bank_info = request.form.get('bank_info', '')
        notes = request.form.get('notes', '')
        tax_rate = float(request.form.get('tax_rate', 10))
        
        # 明細データ取得
        product_names = request.form.getlist('product_name[]')
        brand_names = request.form.getlist('brand_name[]')
        conditions = request.form.getlist('condition[]')
        quantities = request.form.getlist('quantity[]')
        unit_prices = request.form.getlist('unit_price[]')
        
        # 合計計算
        subtotal = 0
        for i in range(len(product_names)):
            if product_names[i]:
                qty_str = quantities[i] if i < len(quantities) else ''
                price_str = unit_prices[i] if i < len(unit_prices) else ''
                qty = int(qty_str) if qty_str else 1
                price = int(price_str) if price_str else 0
                subtotal += qty * price
        
        tax_amount = int(subtotal * tax_rate / 100)
        total_amount = subtotal + tax_amount
        
        try:
            if DATABASE_URL:
                cur.execute('''
                    INSERT INTO admin_kaitori_shoudaku 
                    (document_no, admin_id, company_name, company_address, company_phone, contact_name,
                    issue_date, subtotal, tax_amount, total_amount, tax_rate, payment_method, bank_info, notes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (document_no, current_user.id, company_name, company_address, company_phone, contact_name,
                      issue_date, subtotal, tax_amount, total_amount, tax_rate, payment_method, bank_info, notes))
                kaitori_id = cur.fetchone()['id']
            else:
                cur.execute('''
                    INSERT INTO admin_kaitori_shoudaku 
                    (document_no, admin_id, company_name, company_address, company_phone, contact_name,
                    issue_date, subtotal, tax_amount, total_amount, tax_rate, payment_method, bank_info, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (document_no, current_user.id, company_name, company_address, company_phone, contact_name,
                      issue_date, subtotal, tax_amount, total_amount, tax_rate, payment_method, bank_info, notes))
                kaitori_id = cur.lastrowid
            
            # 明細追加
            for i, product_name in enumerate(product_names):
                if product_name:
                    qty_str = quantities[i] if i < len(quantities) else ''
                    price_str = unit_prices[i] if i < len(unit_prices) else ''
                    qty = int(qty_str) if qty_str else 1
                    price = int(price_str) if price_str else 0
                    amount = qty * price
                    
                    if DATABASE_URL:
                        cur.execute('''
                            INSERT INTO admin_kaitori_shoudaku_items 
                            (kaitori_shoudaku_id, item_no, product_name, brand_name, condition, quantity, unit_price, amount)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ''', (kaitori_id, i+1, product_name, brand_names[i] if i < len(brand_names) else '',
                              conditions[i] if i < len(conditions) else '', qty, price, amount))
                    else:
                        cur.execute('''
                            INSERT INTO admin_kaitori_shoudaku_items 
                            (kaitori_shoudaku_id, item_no, product_name, brand_name, condition, quantity, unit_price, amount)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (kaitori_id, i+1, product_name, brand_names[i] if i < len(brand_names) else '',
                              conditions[i] if i < len(conditions) else '', qty, price, amount))
            
            conn.commit()
        except Exception as e:
            conn.rollback()
            cur.close()
            conn.close()
            flash(f'買取承諾書の作成に失敗しました: {str(e)}', 'error')
            return redirect(url_for('admin_kaitori_shoudaku_add'))
        
        cur.close()
        conn.close()
        
        flash('買取承諾書を作成しました', 'success')
        return redirect(url_for('admin_kaitori_shoudaku_list'))
    
    cur.close()
    conn.close()
    
    today = datetime.now().strftime('%Y-%m-%d')
    return render_template('admin/kaitori_shoudaku_form.html', kaitori=None, items=[], mode='add', today=today)

@app.route('/admin/kaitori-shoudaku/<int:id>')
@login_required
def admin_kaitori_shoudaku_view(id):
    """買取承諾書詳細表示（法人版・管理者用）"""
    if not current_user.is_admin():
        flash('権限がありません', 'error')
        return redirect(url_for('index'))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()
    
    if DATABASE_URL:
        cur.execute('SELECT * FROM admin_kaitori_shoudaku WHERE id = %s', (id,))
        kaitori = cur.fetchone()
        if kaitori:
            kaitori = dict(kaitori)
            cur.execute('SELECT * FROM admin_kaitori_shoudaku_items WHERE kaitori_shoudaku_id = %s ORDER BY item_no', (id,))
            items = [dict(row) for row in cur.fetchall()]
        else:
            items = []
    else:
        cur.execute('SELECT * FROM admin_kaitori_shoudaku WHERE id = ?', (id,))
        row = cur.fetchone()
        if row:
            kaitori = dict(zip([d[0] for d in cur.description], row))
            cur.execute('SELECT * FROM admin_kaitori_shoudaku_items WHERE kaitori_shoudaku_id = ? ORDER BY item_no', (id,))
            items = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        else:
            kaitori = None
            items = []
    
    cur.close()
    if DATABASE_URL:
        conn.close()
    
    if not kaitori:
        flash('買取承諾書が見つかりません', 'error')
        return redirect(url_for('admin_kaitori_shoudaku_list'))
    
    return render_template('admin/kaitori_shoudaku_view.html', kaitori=kaitori, items=items)

@app.route('/admin/kaitori-shoudaku/<int:id>/delete')
@login_required
def admin_kaitori_shoudaku_delete(id):
    """買取承諾書削除（法人版・管理者用）"""
    if not current_user.is_admin():
        flash('権限がありません', 'error')
        return redirect(url_for('index'))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()
    
    if DATABASE_URL:
        cur.execute('DELETE FROM admin_kaitori_shoudaku WHERE id = %s', (id,))
    else:
        cur.execute('DELETE FROM admin_kaitori_shoudaku WHERE id = ?', (id,))
    
    conn.commit()
    cur.close()
    if DATABASE_URL:
        conn.close()
    
    flash('買取承諾書を削除しました', 'success')
    return redirect(url_for('admin_kaitori_shoudaku_list'))

@app.route('/admin/kaitori-shoudaku/<int:id>/pdf')
@login_required
def admin_kaitori_shoudaku_pdf(id):
    """買取承諾書PDF出力（法人版・管理者用）"""
    if not current_user.is_admin():
        flash('権限がありません', 'error')
        return redirect(url_for('index'))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
    else:
        cur = conn.cursor()
    
    if DATABASE_URL:
        cur.execute('SELECT * FROM admin_kaitori_shoudaku WHERE id = %s', (id,))
        kaitori = cur.fetchone()
        if kaitori:
            kaitori = dict(kaitori)
            cur.execute('SELECT * FROM admin_kaitori_shoudaku_items WHERE kaitori_shoudaku_id = %s ORDER BY item_no', (id,))
            items = [dict(row) for row in cur.fetchall()]
        else:
            items = []
    else:
        cur.execute('SELECT * FROM admin_kaitori_shoudaku WHERE id = ?', (id,))
        row = cur.fetchone()
        if row:
            kaitori = dict(zip([d[0] for d in cur.description], row))
            cur.execute('SELECT * FROM admin_kaitori_shoudaku_items WHERE kaitori_shoudaku_id = ? ORDER BY item_no', (id,))
            items = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
        else:
            kaitori = None
            items = []
    
    cur.close()
    if DATABASE_URL:
        conn.close()
    
    if not kaitori:
        flash('買取承諾書が見つかりません', 'error')
        return redirect(url_for('admin_kaitori_shoudaku_list'))
    
    return render_template('pdf/admin_kaitori_shoudaku_pdf.html', kaitori=kaitori, items=items)

# ========== 問い合わせ機能 ==========

# 問い合わせカテゴリ
INQUIRY_CATEGORIES = {
    'general': '一般',
    'technical': '技術的な質問',
    'billing': '請求・支払い',
    'feature': '機能リクエスト',
    'other': 'その他'
}

# 問い合わせステータス
INQUIRY_STATUS = {
    'new': '新着',
    'in_progress': '対応中',
    'resolved': '解決済み',
    'closed': 'クローズ'
}

@app.route('/inquiry')
@login_required
def inquiry_list():
    """ユーザーの問い合わせ一覧"""
    inquiries = []
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # シンプルなクエリで取得
            cur.execute('''
                SELECT id, user_id, category, title, content, image_path, 
                       status, created_at, updated_at
                FROM inquiries
                WHERE user_id = %s
                ORDER BY created_at DESC
            ''', (current_user.id,))
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute('''
                SELECT * FROM inquiries
                WHERE user_id = ?
                ORDER BY created_at DESC
            ''', (current_user.id,))
        
        rows = cur.fetchall()
        for row in rows:
            inq = dict(row)
            # datetime を文字列に変換
            if inq.get('created_at') and hasattr(inq['created_at'], 'strftime'):
                inq['created_at'] = inq['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            if inq.get('updated_at') and hasattr(inq['updated_at'], 'strftime'):
                inq['updated_at'] = inq['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
            inquiries.append(inq)
        
        cur.close()
        conn.close()
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        return f'''
        <html>
        <head><title>Inquiry List Error</title>
        <style>body {{ font-family: monospace; padding: 20px; background: #1a1a2e; color: #eee; }}
        pre {{ background: #16213e; padding: 15px; border-radius: 8px; }}</style>
        </head>
        <body>
        <h1>問い合わせ一覧エラー</h1>
        <p>Error: {str(e)}</p>
        <pre>{error_details}</pre>
        <a href="/" style="color: #4fc3f7;">トップに戻る</a>
        </body></html>
        '''
    
    return render_template('inquiry/list.html', 
                         inquiries=inquiries,
                         categories=INQUIRY_CATEGORIES,
                         statuses=INQUIRY_STATUS)

@app.route('/inquiry/new', methods=['GET', 'POST'])
@login_required
def inquiry_new():
    """新規問い合わせ作成"""
    if request.method == 'POST':
        category = request.form.get('category', 'general')
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        
        if not title or not content:
            flash('タイトルと内容を入力してください', 'error')
            return render_template('inquiry/form.html', categories=INQUIRY_CATEGORIES)
        
        try:
            # 複数画像アップロード処理
            image_paths = []
            if 'images' in request.files:
                images = request.files.getlist('images')
                upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'inquiries')
                os.makedirs(upload_dir, exist_ok=True)
                
                for i, image in enumerate(images):
                    if image and image.filename:
                        # ファイル名をセキュアに（インデックス付き）
                        filename = secure_filename(f"inquiry_{int(time.time())}_{i}_{image.filename}")
                        # ファイルを保存
                        image.save(os.path.join(upload_dir, filename))
                        image_paths.append(f'uploads/inquiries/{filename}')
            
            # 後方互換性のため、単一画像もサポート
            elif 'image' in request.files:
                image = request.files['image']
                if image and image.filename:
                    filename = secure_filename(f"inquiry_{int(time.time())}_{image.filename}")
                    upload_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'inquiries')
                    os.makedirs(upload_dir, exist_ok=True)
                    image.save(os.path.join(upload_dir, filename))
                    image_paths.append(f'uploads/inquiries/{filename}')
            
            # 複数パスをJSON形式で保存
            image_path = json.dumps(image_paths) if image_paths else None
            
            conn = get_db()
            if DATABASE_URL:
                cur = conn.cursor()
                # テーブル存在確認
                cur.execute("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_name = 'inquiries'
                    )
                """)
                if not cur.fetchone()[0]:
                    flash('問い合わせテーブルが存在しません。管理者に連絡してください。', 'error')
                    cur.close()
                    conn.close()
                    return render_template('inquiry/form.html', categories=INQUIRY_CATEGORIES)
                
                cur.execute('''
                    INSERT INTO inquiries (user_id, category, title, content, image_path)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                ''', (current_user.id, category, title, content, image_path))
                new_id = cur.fetchone()[0]
                conn.commit()
            else:
                cur = conn.cursor()
                cur.execute('''
                    INSERT INTO inquiries (user_id, category, title, content, image_path)
                    VALUES (?, ?, ?, ?, ?)
                ''', (current_user.id, category, title, content, image_path))
                new_id = cur.lastrowid
                conn.commit()
            
            cur.close()
            conn.close()
            
            flash(f'お問い合わせを送信しました (ID: {new_id})', 'success')
            return redirect(url_for('inquiry_list'))
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            flash(f'エラーが発生しました: {str(e)}', 'error')
            # エラー詳細を画面に表示
            return f'''
            <html>
            <head><title>Inquiry Error</title>
            <style>body {{ font-family: monospace; padding: 20px; background: #1a1a2e; color: #eee; }}
            pre {{ background: #16213e; padding: 15px; border-radius: 8px; }}</style>
            </head>
            <body>
            <h1>問い合わせ作成エラー</h1>
            <p>Error: {str(e)}</p>
            <pre>{error_details}</pre>
            <a href="/inquiry/new" style="color: #4fc3f7;">戻る</a>
            </body></html>
            '''
    
    return render_template('inquiry/form.html', categories=INQUIRY_CATEGORIES)

@app.route('/inquiry/<int:id>')
@login_required
def inquiry_view(id):
    """問い合わせ詳細"""
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute('SELECT * FROM inquiries WHERE id = %s AND user_id = %s', (id, current_user.id))
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute('SELECT * FROM inquiries WHERE id = ? AND user_id = ?', (id, current_user.id))
        
        inquiry = cur.fetchone()
        if not inquiry:
            cur.close()
            conn.close()
            flash('お問い合わせが見つかりません', 'error')
            return redirect(url_for('inquiry_list'))
        
        inquiry = dict(inquiry)
        # datetime を文字列に変換
        if inquiry.get('created_at') and hasattr(inquiry['created_at'], 'strftime'):
            inquiry['created_at'] = inquiry['created_at'].strftime('%Y-%m-%d %H:%M:%S')
        if inquiry.get('updated_at') and hasattr(inquiry['updated_at'], 'strftime'):
            inquiry['updated_at'] = inquiry['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
        
        # 返信を取得
        if DATABASE_URL:
            cur.execute('''
                SELECT r.*, u.display_name, u.username, u.role
                FROM inquiry_replies r
                JOIN users u ON r.user_id = u.id
                WHERE r.inquiry_id = %s
                ORDER BY r.created_at ASC
            ''', (id,))
        else:
            cur.execute('''
                SELECT r.*, u.display_name, u.username, u.role
                FROM inquiry_replies r
                JOIN users u ON r.user_id = u.id
                WHERE r.inquiry_id = ?
                ORDER BY r.created_at ASC
            ''', (id,))
        
        replies = []
        for row in cur.fetchall():
            reply = dict(row)
            if reply.get('created_at') and hasattr(reply['created_at'], 'strftime'):
                reply['created_at'] = reply['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            replies.append(reply)
        
        cur.close()
        conn.close()
        
        return render_template('inquiry/view.html',
                             inquiry=inquiry,
                             replies=replies,
                             categories=INQUIRY_CATEGORIES,
                             statuses=INQUIRY_STATUS)
    except Exception as e:
        print(f"[ERROR] inquiry_view: {e}", flush=True)
        flash('問い合わせの読み込みでエラーが発生しました', 'error')
        return redirect(url_for('inquiry_list'))

@app.route('/inquiry/<int:id>/delete', methods=['POST'])
@login_required
def inquiry_delete(id):
    """問い合わせを削除"""
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor()
            # 自分の問い合わせか確認
            cur.execute('SELECT id FROM inquiries WHERE id = %s AND user_id = %s', (id, current_user.id))
            if not cur.fetchone():
                flash('お問い合わせが見つかりません', 'error')
                cur.close()
                conn.close()
                return redirect(url_for('inquiry_list'))
            
            # 返信も削除
            cur.execute('DELETE FROM inquiry_replies WHERE inquiry_id = %s', (id,))
            # 問い合わせを削除
            cur.execute('DELETE FROM inquiries WHERE id = %s AND user_id = %s', (id, current_user.id))
            conn.commit()
        else:
            cur = conn.cursor()
            cur.execute('SELECT id FROM inquiries WHERE id = ? AND user_id = ?', (id, current_user.id))
            if not cur.fetchone():
                flash('お問い合わせが見つかりません', 'error')
                cur.close()
                conn.close()
                return redirect(url_for('inquiry_list'))
            
            cur.execute('DELETE FROM inquiry_replies WHERE inquiry_id = ?', (id,))
            cur.execute('DELETE FROM inquiries WHERE id = ? AND user_id = ?', (id, current_user.id))
            conn.commit()
        
        cur.close()
        conn.close()
        flash('お問い合わせを削除しました', 'success')
    except Exception as e:
        flash(f'削除エラー: {str(e)}', 'error')
    
    return redirect(url_for('inquiry_list'))

@app.route('/inquiry/<int:id>/reply', methods=['POST'])
@login_required
def inquiry_reply(id):
    """問い合わせに返信"""
    content = request.form.get('content', '').strip()
    
    if not content:
        flash('返信内容を入力してください', 'error')
        return redirect(url_for('inquiry_view', id=id))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # 自分の問い合わせか確認
        cur.execute('SELECT * FROM inquiries WHERE id = %s AND user_id = %s', (id, current_user.id))
        inquiry = cur.fetchone()
        
        if not inquiry:
            cur.close()
            conn.close()
            flash('お問い合わせが見つかりません', 'error')
            return redirect(url_for('inquiry_list'))
        
        # 返信を追加
        cur.execute('''
            INSERT INTO inquiry_replies (inquiry_id, user_id, content, is_admin_reply)
            VALUES (%s, %s, %s, FALSE)
        ''', (id, current_user.id, content))
        
        # 問い合わせの更新日時を更新
        cur.execute('UPDATE inquiries SET updated_at = CURRENT_TIMESTAMP WHERE id = %s', (id,))
    else:
        cur = conn.cursor()
        cur.execute('SELECT * FROM inquiries WHERE id = ? AND user_id = ?', (id, current_user.id))
        inquiry = cur.fetchone()
        
        if not inquiry:
            cur.close()
            conn.close()
            flash('お問い合わせが見つかりません', 'error')
            return redirect(url_for('inquiry_list'))
        
        cur.execute('''
            INSERT INTO inquiry_replies (inquiry_id, user_id, content, is_admin_reply)
            VALUES (?, ?, ?, 0)
        ''', (id, current_user.id, content))
        
        cur.execute('UPDATE inquiries SET updated_at = CURRENT_TIMESTAMP WHERE id = ?', (id,))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('返信を送信しました', 'success')
    return redirect(url_for('inquiry_view', id=id))

# ========== 管理者：問い合わせ管理 ==========

@app.route('/admin/inquiries')
@login_required
def admin_inquiries():
    """管理者：問い合わせ一覧"""
    if not current_user.is_admin():
        flash('アクセス権限がありません', 'error')
        return redirect(url_for('index'))
    
    status_filter = request.args.get('status', '')
    
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            if status_filter:
                cur.execute('''
                    SELECT i.id, i.user_id, i.category, i.title, i.content, i.status,
                           i.created_at, i.updated_at,
                           u.display_name, u.username,
                           (SELECT COUNT(*) FROM inquiry_replies WHERE inquiry_id = i.id) as reply_count
                    FROM inquiries i
                    JOIN users u ON i.user_id = u.id
                    WHERE i.status = %s
                    ORDER BY 
                        CASE WHEN i.status = 'new' THEN 0 
                             WHEN i.status = 'in_progress' THEN 1 
                             ELSE 2 END,
                        i.updated_at DESC
                ''', (status_filter,))
            else:
                cur.execute('''
                    SELECT i.id, i.user_id, i.category, i.title, i.content, i.status,
                           i.created_at, i.updated_at,
                           u.display_name, u.username,
                           (SELECT COUNT(*) FROM inquiry_replies WHERE inquiry_id = i.id) as reply_count
                    FROM inquiries i
                    JOIN users u ON i.user_id = u.id
                    ORDER BY 
                        CASE WHEN i.status = 'new' THEN 0 
                             WHEN i.status = 'in_progress' THEN 1 
                             ELSE 2 END,
                        i.updated_at DESC
                ''')
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            if status_filter:
                cur.execute('''
                    SELECT i.*, u.display_name, u.username,
                           (SELECT COUNT(*) FROM inquiry_replies WHERE inquiry_id = i.id) as reply_count
                    FROM inquiries i
                    JOIN users u ON i.user_id = u.id
                    WHERE i.status = ?
                    ORDER BY 
                        CASE WHEN i.status = 'new' THEN 0 
                             WHEN i.status = 'in_progress' THEN 1 
                             ELSE 2 END,
                        i.updated_at DESC
                ''', (status_filter,))
            else:
                cur.execute('''
                    SELECT i.*, u.display_name, u.username,
                           (SELECT COUNT(*) FROM inquiry_replies WHERE inquiry_id = i.id) as reply_count
                    FROM inquiries i
                    JOIN users u ON i.user_id = u.id
                    ORDER BY 
                        CASE WHEN i.status = 'new' THEN 0 
                             WHEN i.status = 'in_progress' THEN 1 
                             ELSE 2 END,
                        i.updated_at DESC
                ''')
        
        rows = cur.fetchall()
        inquiries = []
        for row in rows:
            inquiry = dict(row)
            # datetime を文字列に変換
            if inquiry.get('created_at') and hasattr(inquiry['created_at'], 'strftime'):
                inquiry['created_at'] = inquiry['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            if inquiry.get('updated_at') and hasattr(inquiry['updated_at'], 'strftime'):
                inquiry['updated_at'] = inquiry['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
            inquiries.append(inquiry)
        
        # 新着件数を取得（存在するユーザーの問い合わせのみ）
        if DATABASE_URL:
            cur.execute("""
                SELECT COUNT(*) as count FROM inquiries i
                JOIN users u ON i.user_id = u.id
                WHERE i.status = 'new'
            """)
            result = cur.fetchone()
            new_count = result['count'] if result and isinstance(result, dict) else (result[0] if result else 0)
        else:
            cur.execute("""
                SELECT COUNT(*) FROM inquiries i
                JOIN users u ON i.user_id = u.id
                WHERE i.status = 'new'
            """)
            result = cur.fetchone()
            new_count = result[0] if result else 0
        
        cur.close()
        conn.close()
        
        return render_template('admin/inquiries.html',
                             inquiries=inquiries,
                             categories=INQUIRY_CATEGORIES,
                             statuses=INQUIRY_STATUS,
                             status_filter=status_filter,
                             new_count=new_count)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[ERROR] admin_inquiries error: {e}", flush=True)
        flash(f'問い合わせの読み込みでエラーが発生しました: {str(e)[:100]}', 'error')
        return render_template('admin/inquiries.html',
                             inquiries=[],
                             categories=INQUIRY_CATEGORIES,
                             statuses=INQUIRY_STATUS,
                             status_filter='',
                             new_count=0)

@app.route('/admin/inquiry/<int:id>')
@login_required
def admin_inquiry_view(id):
    """管理者：問い合わせ詳細"""
    if not current_user.is_admin():
        flash('アクセス権限がありません', 'error')
        return redirect(url_for('index'))
    
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute('''
                SELECT i.*, u.display_name, u.username, u.email
                FROM inquiries i
                JOIN users u ON i.user_id = u.id
                WHERE i.id = %s
            ''', (id,))
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute('''
                SELECT i.*, u.display_name, u.username, u.email
                FROM inquiries i
                JOIN users u ON i.user_id = u.id
                WHERE i.id = ?
            ''', (id,))
        
        inquiry = cur.fetchone()
        if not inquiry:
            cur.close()
            conn.close()
            flash('お問い合わせが見つかりません', 'error')
            return redirect(url_for('admin_inquiries'))
        
        inquiry = dict(inquiry)
        # datetime を文字列に変換
        if inquiry.get('created_at') and hasattr(inquiry['created_at'], 'strftime'):
            inquiry['created_at'] = inquiry['created_at'].strftime('%Y-%m-%d %H:%M:%S')
        if inquiry.get('updated_at') and hasattr(inquiry['updated_at'], 'strftime'):
            inquiry['updated_at'] = inquiry['updated_at'].strftime('%Y-%m-%d %H:%M:%S')
        
        # 返信を取得
        if DATABASE_URL:
            cur.execute('''
                SELECT r.*, u.display_name, u.username, u.role
                FROM inquiry_replies r
                JOIN users u ON r.user_id = u.id
                WHERE r.inquiry_id = %s
                ORDER BY r.created_at ASC
            ''', (id,))
        else:
            cur.execute('''
                SELECT r.*, u.display_name, u.username, u.role
                FROM inquiry_replies r
                JOIN users u ON r.user_id = u.id
                WHERE r.inquiry_id = ?
                ORDER BY r.created_at ASC
            ''', (id,))
        
        replies = []
        for row in cur.fetchall():
            reply = dict(row)
            if reply.get('created_at') and hasattr(reply['created_at'], 'strftime'):
                reply['created_at'] = reply['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            replies.append(reply)
        
        cur.close()
        conn.close()
        
        return render_template('admin/inquiry_view.html',
                             inquiry=inquiry,
                             replies=replies,
                             categories=INQUIRY_CATEGORIES,
                             statuses=INQUIRY_STATUS)
    except Exception as e:
        print(f"[ERROR] admin_inquiry_view: {e}", flush=True)
        flash('問い合わせの読み込みでエラーが発生しました', 'error')
        return redirect(url_for('admin_inquiries'))

@app.route('/admin/inquiry/<int:id>/reply', methods=['POST'])
@login_required
def admin_inquiry_reply(id):
    """管理者：問い合わせに返信"""
    if not current_user.is_admin():
        flash('アクセス権限がありません', 'error')
        return redirect(url_for('index'))
    
    content = request.form.get('content', '').strip()
    new_status = request.form.get('status', '')
    
    if not content:
        flash('返信内容を入力してください', 'error')
        return redirect(url_for('admin_inquiry_view', id=id))
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        # 返信を追加
        cur.execute('''
            INSERT INTO inquiry_replies (inquiry_id, user_id, content, is_admin_reply)
            VALUES (%s, %s, %s, TRUE)
        ''', (id, current_user.id, content))
        
        # ステータス更新
        if new_status:
            cur.execute('UPDATE inquiries SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s', (new_status, id))
        else:
            cur.execute('UPDATE inquiries SET updated_at = CURRENT_TIMESTAMP WHERE id = %s', (id,))
    else:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO inquiry_replies (inquiry_id, user_id, content, is_admin_reply)
            VALUES (?, ?, ?, 1)
        ''', (id, current_user.id, content))
        
        if new_status:
            cur.execute('UPDATE inquiries SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (new_status, id))
        else:
            cur.execute('UPDATE inquiries SET updated_at = CURRENT_TIMESTAMP WHERE id = ?', (id,))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('返信を送信しました', 'success')
    return redirect(url_for('admin_inquiry_view', id=id))

@app.route('/admin/inquiry/<int:id>/status', methods=['POST'])
@login_required
def admin_inquiry_status(id):
    """管理者：ステータス変更"""
    if not current_user.is_admin():
        return jsonify({'success': False, 'error': 'アクセス権限がありません'}), 403
    
    data = request.get_json()
    new_status = data.get('status', '')
    
    if new_status not in INQUIRY_STATUS:
        return jsonify({'success': False, 'error': '無効なステータスです'}), 400
    
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute('UPDATE inquiries SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s', (new_status, id))
    else:
        cur = conn.cursor()
        cur.execute('UPDATE inquiries SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (new_status, id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    return jsonify({'success': True, 'status': new_status, 'status_label': INQUIRY_STATUS[new_status]})

# 未読問い合わせ件数を取得するヘルパー関数
def get_unread_inquiry_count():
    """管理者向け：新着問い合わせ件数を取得"""
    if not current_user.is_authenticated or not current_user.is_admin():
        return 0
    
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor()
            # 一覧と同じ条件でカウント（存在するユーザーの問い合わせのみ、クローズを除外）
            cur.execute("""
                SELECT COUNT(*) as count FROM inquiries i
                JOIN users u ON i.user_id = u.id
                WHERE i.status = 'new'
            """)
            count = cur.fetchone()[0]
        else:
            cur = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) FROM inquiries i
                JOIN users u ON i.user_id = u.id
                WHERE i.status = 'new'
            """)
            count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    except Exception as e:
        # テーブルが存在しない場合など
        return 0


# 未処理商品処分申請件数を取得するヘルパー関数
def get_pending_disposal_count():
    """管理者向け：未処理の商品処分申請件数を取得"""
    if not current_user.is_authenticated or not current_user.is_admin():
        return 0
    
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM item_disposal_requests WHERE status = 'pending'")
            count = cur.fetchone()[0]
        else:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM item_disposal_requests WHERE status = 'pending'")
            count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    except Exception as e:
        # テーブルが存在しない場合など
        return 0


# テンプレートで使えるようにコンテキストプロセッサに追加
@app.context_processor
def inject_inquiry_count():
    return dict(
        get_unread_inquiry_count=get_unread_inquiry_count,
        get_pending_disposal_count=get_pending_disposal_count
    )

# ========== 売却申請機能 ==========

# 未処理の売却申請件数を取得
def get_pending_sale_request_count():
    try:
        conn = get_db()
        cur = conn.cursor()
        if DATABASE_URL:
            cur.execute("SELECT COUNT(*) FROM sale_requests WHERE status = 'pending'")
        else:
            cur.execute("SELECT COUNT(*) FROM sale_requests WHERE status = 'pending'")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    except Exception as e:
        return 0

# テンプレートで使えるようにコンテキストプロセッサに追加
@app.context_processor
def inject_sale_request_count():
    return dict(get_pending_sale_request_count=get_pending_sale_request_count)

# ユーザー：売却申請送信
@app.route('/sale-request/submit/<int:item_id>', methods=['POST'])
@login_required
def submit_sale_request(item_id):
    sale_price = request.form.get('sale_price', type=int)
    qr_image = request.files.get('qr_image')
    
    if not sale_price or sale_price <= 0:
        flash('売上金額を正しく入力してください', 'error')
        return redirect(url_for('index'))
    
    conn = get_db()
    cur = conn.cursor()
    
    # 商品が存在し、ユーザーのものか確認
    if DATABASE_URL:
        cur.execute("SELECT * FROM merchandise WHERE id = %s AND user_id = %s", (item_id, current_user.id))
    else:
        cur.execute("SELECT * FROM merchandise WHERE id = ? AND user_id = ?", (item_id, current_user.id))
    
    item = cur.fetchone()
    if not item:
        flash('商品が見つかりません', 'error')
        cur.close()
        conn.close()
        return redirect(url_for('index'))
    
    # 既に申請中かチェック
    if DATABASE_URL:
        cur.execute("SELECT * FROM sale_requests WHERE merchandise_id = %s AND status = 'pending'", (item_id,))
    else:
        cur.execute("SELECT * FROM sale_requests WHERE merchandise_id = ? AND status = 'pending'", (item_id,))
    
    if cur.fetchone():
        flash('この商品は既に売却申請中です', 'error')
        cur.close()
        conn.close()
        return redirect(url_for('index'))
    
    # QR画像の保存
    qr_image_path = None
    if qr_image and qr_image.filename:
        filename = secure_filename(qr_image.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_')
        filename = timestamp + filename
        upload_folder = os.path.join(app.root_path, 'static', 'uploads', 'qr')
        os.makedirs(upload_folder, exist_ok=True)
        qr_image.save(os.path.join(upload_folder, filename))
        qr_image_path = 'uploads/qr/' + filename
    
    # 売却申請を登録
    if DATABASE_URL:
        cur.execute('''
            INSERT INTO sale_requests (merchandise_id, user_id, sale_price, qr_image_path, status)
            VALUES (%s, %s, %s, %s, 'pending')
        ''', (item_id, current_user.id, sale_price, qr_image_path))
    else:
        cur.execute('''
            INSERT INTO sale_requests (merchandise_id, user_id, sale_price, qr_image_path, status)
            VALUES (?, ?, ?, ?, 'pending')
        ''', (item_id, current_user.id, sale_price, qr_image_path))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('売却申請を送信しました。管理者の確認をお待ちください。', 'success')
    return redirect(url_for('index'))

# 管理者：発送商品受信BOX（売却申請一覧）
@app.route('/admin/sale-requests')
@login_required
def admin_sale_requests():
    if not current_user.is_admin():
        flash('管理者権限が必要です', 'error')
        return redirect(url_for('index'))
    
    requests_list = []
    pending_count = 0
    approved_count = 0
    rejected_count = 0
    
    try:
        conn = get_db()
        
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # メインクエリ
            cur.execute('''
                SELECT sr.*, m.product_name, m.brand_name, m.photo_path, m.purchase_price,
                       u.username, u.display_name as user_display_name,
                       p.username as processor_name, p.display_name as processor_display_name
                FROM sale_requests sr
                JOIN merchandise m ON sr.merchandise_id = m.id
                JOIN users u ON sr.user_id = u.id
                LEFT JOIN users p ON sr.processed_by = p.id
                ORDER BY 
                    CASE WHEN sr.status = 'pending' THEN 0 ELSE 1 END,
                    sr.created_at DESC
            ''')
            requests_list = cur.fetchall()
            
            # 統計情報
            cur.execute("SELECT COUNT(*) as count FROM sale_requests WHERE status = 'pending'")
            pending_count = cur.fetchone()['count']
            cur.execute("SELECT COUNT(*) as count FROM sale_requests WHERE status = 'approved'")
            approved_count = cur.fetchone()['count']
            cur.execute("SELECT COUNT(*) as count FROM sale_requests WHERE status = 'rejected'")
            rejected_count = cur.fetchone()['count']
        else:
            cur = conn.cursor()
            cur.execute('''
                SELECT sr.*, m.product_name, m.brand_name, m.photo_path, m.purchase_price,
                       u.username, u.display_name as user_display_name,
                       p.username as processor_name, p.display_name as processor_display_name
                FROM sale_requests sr
                JOIN merchandise m ON sr.merchandise_id = m.id
                JOIN users u ON sr.user_id = u.id
                LEFT JOIN users p ON sr.processed_by = p.id
                ORDER BY 
                    CASE WHEN sr.status = 'pending' THEN 0 ELSE 1 END,
                    sr.created_at DESC
            ''')
            requests_list = cur.fetchall()
            
            # 統計情報
            cur.execute("SELECT COUNT(*) FROM sale_requests WHERE status = 'pending'")
            pending_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM sale_requests WHERE status = 'approved'")
            approved_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM sale_requests WHERE status = 'rejected'")
            rejected_count = cur.fetchone()[0]
        
        cur.close()
        conn.close()
    except Exception as e:
        # テーブルが存在しない場合など - 空のリストを返す
        print(f"Error in admin_sale_requests: {e}")
        import traceback
        traceback.print_exc()
    
    return render_template('admin/sale_requests.html', 
                         requests=requests_list,
                         pending_count=pending_count,
                         approved_count=approved_count,
                         rejected_count=rejected_count)

# 管理者：売却申請承認
@app.route('/admin/sale-request/<int:request_id>/approve', methods=['POST'])
@login_required
def approve_sale_request(request_id):
    if not current_user.is_admin():
        flash('管理者権限が必要です', 'error')
        return redirect(url_for('index'))
    
    admin_note = request.form.get('admin_note', '')
    
    try:
        conn = get_db()
        
        # 申請情報を取得（sale_price, merchandise_idが必要）
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT id, merchandise_id, sale_price FROM sale_requests WHERE id = %s", (request_id,))
            sale_request = cur.fetchone()
            cur.close()
            
            if not sale_request:
                flash('申請が見つかりません', 'error')
                conn.close()
                return redirect(url_for('admin_sale_requests'))
            
            sale_price = sale_request['sale_price']
            merchandise_id = sale_request['merchandise_id']
            
            # 通常のカーソルでUPDATE実行
            cur = conn.cursor()
            cur.execute('''
                UPDATE sale_requests 
                SET status = 'approved', processed_at = %s, processed_by = %s, admin_note = %s
                WHERE id = %s
            ''', (datetime.now(), current_user.id, admin_note, request_id))
            
            # 商品のステータスを売却済みに更新、売上金も更新
            cur.execute('''
                UPDATE merchandise 
                SET is_listed = TRUE, sale_price = %s, sale_date = %s, updated_at = %s, updated_by = %s
                WHERE id = %s
            ''', (sale_price, datetime.now().date(), datetime.now(), current_user.id, merchandise_id))
        else:
            cur = conn.cursor()
            cur.row_factory = sqlite3.Row
            cur.execute("SELECT id, merchandise_id, sale_price FROM sale_requests WHERE id = ?", (request_id,))
            sale_request = cur.fetchone()
            
            if not sale_request:
                flash('申請が見つかりません', 'error')
                cur.close()
                conn.close()
                return redirect(url_for('admin_sale_requests'))
            
            sale_price = sale_request['sale_price']
            merchandise_id = sale_request['merchandise_id']
            
            cur.execute('''
                UPDATE sale_requests 
                SET status = 'approved', processed_at = ?, processed_by = ?, admin_note = ?
                WHERE id = ?
            ''', (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), current_user.id, admin_note, request_id))
            
            # 商品のステータスを売却済みに更新、売上金も更新
            cur.execute('''
                UPDATE merchandise 
                SET is_listed = 1, sale_price = ?, sale_date = ?, updated_at = ?, updated_by = ?
                WHERE id = ?
            ''', (sale_price, datetime.now().strftime('%Y-%m-%d'), datetime.now().strftime('%Y-%m-%d %H:%M:%S'), current_user.id, merchandise_id))
        
        conn.commit()
        cur.close()
        conn.close()
        
        flash('売却申請を承認しました。商品ステータスが売却済みに更新されました。', 'success')
    except Exception as e:
        print(f"Error in approve_sale_request: {e}")
        import traceback
        traceback.print_exc()
        flash(f'承認処理でエラーが発生しました: {str(e)}', 'error')
    
    return redirect(url_for('admin_sale_requests'))

# 管理者：売却申請却下
@app.route('/admin/sale-request/<int:request_id>/reject', methods=['POST'])
@login_required
def reject_sale_request(request_id):
    if not current_user.is_admin():
        flash('管理者権限が必要です', 'error')
        return redirect(url_for('index'))
    
    admin_note = request.form.get('admin_note', '')
    
    conn = get_db()
    
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute('''
            UPDATE sale_requests 
            SET status = 'rejected', processed_at = %s, processed_by = %s, admin_note = %s
            WHERE id = %s
        ''', (datetime.now(), current_user.id, admin_note, request_id))
    else:
        cur = conn.cursor()
        cur.execute('''
            UPDATE sale_requests 
            SET status = 'rejected', processed_at = ?, processed_by = ?, admin_note = ?
            WHERE id = ?
        ''', (datetime.now(), current_user.id, admin_note, request_id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('売却申請を却下しました', 'info')
    return redirect(url_for('admin_sale_requests'))

# ユーザー：売却申請の修正
@app.route('/sale-request/edit/<int:request_id>', methods=['POST'])
@login_required
def edit_sale_request(request_id):
    sale_price = request.form.get('sale_price', type=int)
    qr_image = request.files.get('qr_image')
    
    if not sale_price or sale_price <= 0:
        flash('売上金額を正しく入力してください', 'error')
        return redirect(url_for('index'))
    
    conn = get_db()
    cur = conn.cursor()
    
    # 申請情報を取得し、ユーザーのものか確認
    if DATABASE_URL:
        cur.execute("SELECT * FROM sale_requests WHERE id = %s AND user_id = %s AND status = 'pending'", 
                   (request_id, current_user.id))
    else:
        cur.execute("SELECT * FROM sale_requests WHERE id = ? AND user_id = ? AND status = 'pending'", 
                   (request_id, current_user.id))
    
    sale_request = cur.fetchone()
    if not sale_request:
        flash('申請が見つからないか、既に処理済みです', 'error')
        cur.close()
        conn.close()
        return redirect(url_for('index'))
    
    sale_request_dict = dict(sale_request)
    
    # QR画像の保存（新しい画像がある場合のみ）
    qr_image_path = sale_request_dict.get('qr_image_path')
    if qr_image and qr_image.filename:
        filename = secure_filename(qr_image.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_')
        filename = timestamp + filename
        upload_folder = os.path.join(app.root_path, 'static', 'uploads', 'qr')
        os.makedirs(upload_folder, exist_ok=True)
        qr_image.save(os.path.join(upload_folder, filename))
        qr_image_path = 'uploads/qr/' + filename
    
    # 売却申請を更新
    if DATABASE_URL:
        cur.execute('''
            UPDATE sale_requests 
            SET sale_price = %s, qr_image_path = %s
            WHERE id = %s
        ''', (sale_price, qr_image_path, request_id))
    else:
        cur.execute('''
            UPDATE sale_requests 
            SET sale_price = ?, qr_image_path = ?
            WHERE id = ?
        ''', (sale_price, qr_image_path, request_id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('売却申請を修正しました', 'success')
    return redirect(url_for('index'))

# ユーザー：売却申請のキャンセル
@app.route('/sale-request/cancel/<int:request_id>', methods=['POST'])
@login_required
def cancel_sale_request(request_id):
    conn = get_db()
    cur = conn.cursor()
    
    # 申請情報を取得し、ユーザーのものか確認
    if DATABASE_URL:
        cur.execute("SELECT * FROM sale_requests WHERE id = %s AND user_id = %s AND status = 'pending'", 
                   (request_id, current_user.id))
    else:
        cur.execute("SELECT * FROM sale_requests WHERE id = ? AND user_id = ? AND status = 'pending'", 
                   (request_id, current_user.id))
    
    if not cur.fetchone():
        flash('申請が見つからないか、既に処理済みです', 'error')
        cur.close()
        conn.close()
        return redirect(url_for('index'))
    
    # 申請を削除
    if DATABASE_URL:
        cur.execute("DELETE FROM sale_requests WHERE id = %s", (request_id,))
    else:
        cur.execute("DELETE FROM sale_requests WHERE id = ?", (request_id,))
    
    conn.commit()
    cur.close()
    conn.close()
    
    flash('売却申請をキャンセルしました', 'info')
    return redirect(url_for('index'))

# =====================================================
# 販売代行サービス機能
# =====================================================

SALES_AGENCY_SERVICE_TYPES = {
    'wholesale': '業者卸販売サービス',
    'simultaneous': '同時出品サービス',
    'auction': '業者オークション出品'
}

SALES_AGENCY_STATUS = {
    'pending': '審査中',
    'approved': '承認',
    'rejected': '却下'
}

def get_pending_sales_agency_count():
    """管理者向け：未処理の販売代行申請件数を取得"""
    try:
        if not current_user.is_authenticated or not current_user.is_admin():
            return 0
        
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM sales_agency_requests WHERE status = 'pending'")
        else:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM sales_agency_requests WHERE status = 'pending'")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return count
    except Exception as e:
        print(f"get_pending_sales_agency_count error: {e}")
        return 0

@app.context_processor
def inject_sales_agency_count():
    return dict(get_pending_sales_agency_count=get_pending_sales_agency_count)

@app.route('/sales-agency/apply', methods=['POST'])
@login_required
def sales_agency_apply():
    """販売代行サービス申請"""
    service_type = request.form.get('service_type')
    merchandise_ids = request.form.getlist('merchandise_ids')
    print(f"[DEBUG] sales_agency_apply: service_type={service_type}, merchandise_ids={merchandise_ids}", flush=True)
    
    if not service_type or service_type not in SALES_AGENCY_SERVICE_TYPES:
        flash('サービス種別を選択してください', 'error')
        return redirect(url_for('index'))
    
    if not merchandise_ids:
        flash('商品を選択してください', 'error')
        return redirect(url_for('index'))
    
    try:
        conn = get_db()
        print(f"[DEBUG] sales_agency_apply: got DB connection, DATABASE_URL is set: {DATABASE_URL is not None}", flush=True)
        if DATABASE_URL:
            cur = conn.cursor()
            print(f"[DEBUG] sales_agency_apply: inserting into sales_agency_requests for user_id={current_user.id}, service_type={service_type}", flush=True)
            # 申請を作成
            cur.execute('''
                INSERT INTO sales_agency_requests (user_id, service_type)
                VALUES (%s, %s) RETURNING id
            ''', (current_user.id, service_type))
            result = cur.fetchone()
            print(f"[DEBUG] sales_agency_apply: INSERT result = {result}", flush=True)
            request_id = result[0]
            print(f"[DEBUG] sales_agency_apply: request_id = {request_id}", flush=True)
            
            # 商品を紐付け
            for m_id in merchandise_ids:
                print(f"[DEBUG] sales_agency_apply: inserting item request_id={request_id}, merchandise_id={m_id}", flush=True)
                cur.execute('''
                    INSERT INTO sales_agency_request_items (request_id, merchandise_id)
                    VALUES (%s, %s)
                ''', (request_id, int(m_id)))
        else:
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO sales_agency_requests (user_id, service_type)
                VALUES (?, ?)
            ''', (current_user.id, service_type))
            request_id = cur.lastrowid
            
            for m_id in merchandise_ids:
                cur.execute('''
                    INSERT INTO sales_agency_request_items (request_id, merchandise_id)
                    VALUES (?, ?)
                ''', (request_id, int(m_id)))
        
        conn.commit()
        print(f"[DEBUG] sales_agency_apply: request_id={request_id} created successfully", flush=True)
        
        # 確認用：挿入後にデータを再取得して確認
        if DATABASE_URL:
            cur.execute("SELECT id, user_id, service_type, status FROM sales_agency_requests WHERE id = %s", (request_id,))
        else:
            cur.execute("SELECT id, user_id, service_type, status FROM sales_agency_requests WHERE id = ?", (request_id,))
        check_row = cur.fetchone()
        print(f"[DEBUG] sales_agency_apply: verification after insert: {check_row}", flush=True)
        
        cur.close()
        conn.close()
        
        # 管理者にLINE通知
        try:
            admin_conn = get_db()
            if DATABASE_URL:
                admin_cur = admin_conn.cursor(cursor_factory=RealDictCursor)
                admin_cur.execute("SELECT line_user_id FROM users WHERE role IN ('admin', 'owner') AND line_user_id IS NOT NULL")
            else:
                admin_cur = admin_conn.cursor()
                admin_cur.execute("SELECT line_user_id FROM users WHERE role IN ('admin', 'owner') AND line_user_id IS NOT NULL")
            
            admins = admin_cur.fetchall()
            admin_cur.close()
            admin_conn.close()
            
            service_name = SALES_AGENCY_SERVICE_TYPES.get(service_type, service_type)
            message = f"【販売代行申請】\n{current_user.display_name or current_user.username}さんから{service_name}の申請がありました。\n商品数: {len(merchandise_ids)}点"
            
            for admin in admins:
                line_id = admin['line_user_id'] if isinstance(admin, dict) else admin[0]
                if line_id:
                    send_line_push(line_id, message)
        except Exception as e:
            print(f"LINE notification error: {e}")
        
        flash(f'{SALES_AGENCY_SERVICE_TYPES[service_type]}の申請を送信しました', 'success')
    except Exception as e:
        print(f"Sales agency apply error: {e}")
        flash('申請に失敗しました', 'error')
    
    return redirect(url_for('index'))

@app.route('/sales-agency/my-requests')
@login_required
def sales_agency_my_requests():
    """ユーザーの販売代行申請履歴"""
    requests = []
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute('''
                SELECT sar.id, sar.user_id, sar.service_type, sar.status, sar.admin_note,
                       sar.created_at, sar.processed_at, sar.processed_by, sar.result_notified,
                       u.display_name as processor_name
                FROM sales_agency_requests sar
                LEFT JOIN users u ON sar.processed_by = u.id
                WHERE sar.user_id = %s
                ORDER BY sar.created_at DESC
            ''', (current_user.id,))
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute('''
                SELECT sar.*, u.display_name as processor_name
                FROM sales_agency_requests sar
                LEFT JOIN users u ON sar.processed_by = u.id
                WHERE sar.user_id = ?
                ORDER BY sar.created_at DESC
            ''', (current_user.id,))
        
        requests_raw = cur.fetchall()
        
        for req in requests_raw:
            req_dict = dict(req)
            # datetime を文字列に変換
            if req_dict.get('created_at') and hasattr(req_dict['created_at'], 'strftime'):
                req_dict['created_at'] = req_dict['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            if req_dict.get('processed_at') and hasattr(req_dict['processed_at'], 'strftime'):
                req_dict['processed_at'] = req_dict['processed_at'].strftime('%Y-%m-%d %H:%M:%S')
            
            # 関連商品を取得
            if DATABASE_URL:
                cur.execute('''
                    SELECT m.id, m.product_name, m.brand_name, m.listing_price, m.photo_path
                    FROM sales_agency_request_items sari
                    JOIN merchandise m ON sari.merchandise_id = m.id
                    WHERE sari.request_id = %s
                ''', (req_dict['id'],))
            else:
                cur.execute('''
                    SELECT m.id, m.product_name, m.brand_name, m.listing_price, m.photo_path
                    FROM sales_agency_request_items sari
                    JOIN merchandise m ON sari.merchandise_id = m.id
                    WHERE sari.request_id = ?
                ''', (req_dict['id'],))
            
            request_items = [dict(item) for item in cur.fetchall()]
            req_dict['request_items'] = request_items
            requests.append(req_dict)
        
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] sales_agency_my_requests: {e}", flush=True)
        import traceback
        traceback.print_exc()
    
    return render_template('sales_agency_requests.html',
                         requests=requests,
                         service_types=SALES_AGENCY_SERVICE_TYPES,
                         statuses=SALES_AGENCY_STATUS)

@app.route('/admin/sales-agency-requests')
@login_required
def admin_sales_agency_requests():
    """管理者：販売代行サービス受信BOX"""
    if not current_user.is_admin():
        flash('アクセス権限がありません', 'error')
        return redirect(url_for('index'))
    
    status_filter = request.args.get('status', 'all')
    print(f"[DEBUG] admin_sales_agency_requests called, status_filter={status_filter}", flush=True)
    
    # デフォルト値
    requests_list = []
    stats = {'pending': 0, 'approved': 0, 'rejected': 0}
    
    try:
        conn = get_db()
        if DATABASE_URL:
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            if status_filter == 'processed':
                # 処理済み（承認・却下）のみ表示
                cur.execute('''
                    SELECT sar.*, u.display_name as user_name, u.username,
                           p.display_name as processor_name
                    FROM sales_agency_requests sar
                    JOIN users u ON sar.user_id = u.id
                    LEFT JOIN users p ON sar.processed_by = p.id
                    WHERE sar.status IN ('approved', 'rejected')
                    ORDER BY sar.processed_at DESC
                ''')
            elif status_filter != 'all':
                cur.execute('''
                    SELECT sar.*, u.display_name as user_name, u.username,
                           p.display_name as processor_name
                    FROM sales_agency_requests sar
                    JOIN users u ON sar.user_id = u.id
                    LEFT JOIN users p ON sar.processed_by = p.id
                    WHERE sar.status = %s
                    ORDER BY sar.created_at DESC
                ''', (status_filter,))
            else:
                cur.execute('''
                    SELECT sar.*, u.display_name as user_name, u.username,
                           p.display_name as processor_name
                    FROM sales_agency_requests sar
                    JOIN users u ON sar.user_id = u.id
                    LEFT JOIN users p ON sar.processed_by = p.id
                    ORDER BY 
                        CASE WHEN sar.status = 'pending' THEN 0 ELSE 1 END,
                        sar.created_at DESC
                ''')
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            
            if status_filter == 'processed':
                # 処理済み（承認・却下）のみ表示
                cur.execute('''
                    SELECT sar.*, u.display_name as user_name, u.username,
                           p.display_name as processor_name
                    FROM sales_agency_requests sar
                    JOIN users u ON sar.user_id = u.id
                    LEFT JOIN users p ON sar.processed_by = p.id
                    WHERE sar.status IN ('approved', 'rejected')
                    ORDER BY sar.processed_at DESC
                ''')
            elif status_filter != 'all':
                cur.execute('''
                    SELECT sar.*, u.display_name as user_name, u.username,
                           p.display_name as processor_name
                    FROM sales_agency_requests sar
                    JOIN users u ON sar.user_id = u.id
                    LEFT JOIN users p ON sar.processed_by = p.id
                    WHERE sar.status = ?
                    ORDER BY sar.created_at DESC
                ''', (status_filter,))
            else:
                cur.execute('''
                    SELECT sar.*, u.display_name as user_name, u.username,
                           p.display_name as processor_name
                    FROM sales_agency_requests sar
                    JOIN users u ON sar.user_id = u.id
                    LEFT JOIN users p ON sar.processed_by = p.id
                    ORDER BY 
                        CASE WHEN sar.status = 'pending' THEN 0 ELSE 1 END,
                        sar.created_at DESC
                ''')
        
        requests_raw = cur.fetchall()
        print(f"[DEBUG] admin_sales_agency_requests: fetched {len(requests_raw)} requests", flush=True)
        
        for req in requests_raw:
            req_dict = dict(req)
            # datetime を文字列に変換
            if req_dict.get('created_at') and hasattr(req_dict['created_at'], 'strftime'):
                req_dict['created_at'] = req_dict['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            if req_dict.get('processed_at') and hasattr(req_dict['processed_at'], 'strftime'):
                req_dict['processed_at'] = req_dict['processed_at'].strftime('%Y-%m-%d %H:%M:%S')
            
            # 関連商品を取得
            if DATABASE_URL:
                cur.execute('''
                    SELECT m.id, m.product_name, m.brand_name, m.listing_price, m.photo_path
                    FROM sales_agency_request_items sari
                    JOIN merchandise m ON sari.merchandise_id = m.id
                    WHERE sari.request_id = %s
                ''', (req_dict['id'],))
            else:
                cur.execute('''
                    SELECT m.id, m.product_name, m.brand_name, m.listing_price, m.photo_path
                    FROM sales_agency_request_items sari
                    JOIN merchandise m ON sari.merchandise_id = m.id
                    WHERE sari.request_id = ?
                ''', (req_dict['id'],))
            
            merchandise_items = [dict(item) for item in cur.fetchall()]
            req_dict['merchandise_items'] = merchandise_items
            requests_list.append(req_dict)
        
        # 統計を取得
        if DATABASE_URL:
            cur.execute("SELECT status, COUNT(*) as cnt FROM sales_agency_requests GROUP BY status")
            for row in cur.fetchall():
                row_dict = dict(row)
                if row_dict.get('status') in stats:
                    stats[row_dict['status']] = row_dict.get('cnt', 0)
        else:
            cur.execute("SELECT status, COUNT(*) as cnt FROM sales_agency_requests GROUP BY status")
            for row in cur.fetchall():
                row_dict = dict(row)
                if row_dict.get('status') in stats:
                    stats[row_dict['status']] = row_dict.get('cnt', 0)
        
        cur.close()
        conn.close()
        
        return render_template('admin/sales_agency_requests.html',
                             requests=requests_list,
                             stats=stats,
                             status_filter=status_filter,
                             service_types=SALES_AGENCY_SERVICE_TYPES,
                             statuses=SALES_AGENCY_STATUS)
    except Exception as e:
        import traceback
        print(f"Sales agency requests error: {e}")
        traceback.print_exc()
        # エラーでも空の状態で表示
        return render_template('admin/sales_agency_requests.html',
                             requests=[],
                             stats={'pending': 0, 'approved': 0, 'rejected': 0},
                             status_filter=status_filter,
                             service_types=SALES_AGENCY_SERVICE_TYPES,
                             statuses=SALES_AGENCY_STATUS)

@app.route('/admin/sales-agency-requests/<int:id>/process', methods=['POST'])
@login_required
def admin_sales_agency_process(id):
    """管理者：販売代行申請を処理（承認/却下）"""
    if not current_user.is_admin():
        return jsonify({'success': False, 'error': 'アクセス権限がありません'}), 403
    
    data = request.get_json()
    action = data.get('action')  # 'approve' or 'reject'
    admin_note = data.get('admin_note', '')
    
    if action not in ['approve', 'reject']:
        return jsonify({'success': False, 'error': '無効なアクションです'}), 400
    
    new_status = 'approved' if action == 'approve' else 'rejected'
    
    try:
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # 申請を取得
            cur.execute('SELECT * FROM sales_agency_requests WHERE id = %s', (id,))
            req = cur.fetchone()
            if not req:
                return jsonify({'success': False, 'error': '申請が見つかりません'}), 404
            
            # ステータス更新
            cur.execute('''
                UPDATE sales_agency_requests
                SET status = %s, admin_note = %s, processed_at = %s, processed_by = %s
                WHERE id = %s
            ''', (new_status, admin_note, datetime.now(), current_user.id, id))
            
            # ユーザー情報を取得
            cur.execute('SELECT * FROM users WHERE id = %s', (req['user_id'],))
            user = cur.fetchone()
        else:
            cur = conn.cursor()
            cur.execute('SELECT * FROM sales_agency_requests WHERE id = ?', (id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'success': False, 'error': '申請が見つかりません'}), 404
            req = dict(zip([d[0] for d in cur.description], row))
            
            cur.execute('''
                UPDATE sales_agency_requests
                SET status = ?, admin_note = ?, processed_at = ?, processed_by = ?
                WHERE id = ?
            ''', (new_status, admin_note, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), current_user.id, id))
            
            cur.execute('SELECT * FROM users WHERE id = ?', (req['user_id'],))
            row = cur.fetchone()
            user = dict(zip([d[0] for d in cur.description], row)) if row else None
        
        conn.commit()
        cur.close()
        conn.close()
        
        # ユーザーにLINE通知
        if user:
            line_user_id = user.get('line_user_id') if isinstance(user, dict) else user[7]
            if line_user_id:
                service_name = SALES_AGENCY_SERVICE_TYPES.get(req['service_type'], req['service_type'])
                status_text = '承認' if new_status == 'approved' else '却下'
                message = f"【販売代行申請結果】\n{service_name}の申請が{status_text}されました。"
                if admin_note:
                    message += f"\n\n備考: {admin_note}"
                send_line_push(line_user_id, message)
        
        return jsonify({'success': True, 'status': new_status})
    except Exception as e:
        print(f"Sales agency process error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# アプリ起動時にスケジューラーを初期化
# Gunicorn等で複数ワーカーの場合、重複起動を防ぐ
import atexit

if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
    init_scheduler()

# アプリ終了時にスケジューラーを停止
@atexit.register
def shutdown_scheduler():
    global scheduler
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
        print(f"[{datetime.now()}] Scheduler stopped")

@app.route('/item/<int:id>/download_all')
@login_required
def download_all_images(id):
    import io
    import zipfile
    
    # DB接続
    conn = get_db()
    item = None
    
    try:
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM merchandise WHERE id = %s", (id,))
            item_data = cur.fetchone()
            if item_data:
                item = dict(item_data)
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM merchandise WHERE id = ?", (id,))
            item_data = cur.fetchone()
            if item_data:
                item = dict(item_data)
    except Exception as e:
        print(f"DB Error: {e}")
    finally:
        if 'cur' in locals():
            cur.close()
        conn.close()
    
    if not item:
        flash('商品が見つかりません', 'error')
        return redirect(request.referrer or url_for('index'))
        
    photos = []
    if item.get('photo_path'):
        photos.append(item['photo_path'])
        
    if item.get('additional_photos'):
        try:
            additional = json.loads(item['additional_photos'])
            if isinstance(additional, list):
                photos.extend(additional)
        except:
            # JSONデコード失敗時、カンマ区切りとして処理（後方互換性）
            if isinstance(item['additional_photos'], str):
                photos.extend([p.strip() for p in item['additional_photos'].split(',') if p.strip()])
            
    if not photos:
        flash('画像がありません', 'warning')
        return redirect(request.referrer or url_for('index'))

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for i, photo_path in enumerate(photos):
            filename = os.path.basename(photo_path)
            abs_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            
            if os.path.exists(abs_path):
                ext = os.path.splitext(filename)[1]
                zip_filename = f"image_{i+1:03d}{ext}"
                try:
                    zf.write(abs_path, zip_filename)
                except Exception as e:
                    print(f"Error adding file to zip: {e}")
    
    memory_file.seek(0)
    
    # ファイル名生成（英数字のみ）
    safe_name = "".join([c for c in item.get('product_name', '') if c.isalnum() or c in (' ', '-', '_')]).strip()
    if not safe_name:
        safe_name = f"item_{id}"
        
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f"{safe_name}_images.zip"
    )

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

