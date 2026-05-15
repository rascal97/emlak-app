from flask import Flask, render_template, request, redirect, url_for, flash, g
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2, psycopg2.extras, psycopg2.errors
import os, json, base64
from datetime import datetime, date, timedelta

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import atexit
    _SCHED_AVAILABLE = True
except ImportError:
    _SCHED_AVAILABLE = False

try:
    from pywebpush import webpush, WebPushException
    _PUSH_LIB = True
except ImportError:
    _PUSH_LIB = False

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'emlak_pro_gizli_2024')

_DB_URL = os.environ.get('DATABASE_URL', 'postgresql://localhost/emlak')
if _DB_URL.startswith('postgres://'):
    _DB_URL = _DB_URL.replace('postgres://', 'postgresql://', 1)

# ── VAPID push bildirimi anahtarları ───────────────────────────────────────
VAPID_PRIVATE_KEY  = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_PUBLIC_KEY   = os.environ.get('VAPID_PUBLIC_KEY', '')

if _PUSH_LIB and not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY):
    try:
        from py_vapid import Vapid
        from cryptography.hazmat.primitives import serialization
        _v = Vapid()
        _v.generate_keys()
        _priv_num = _v.private_key.private_numbers().private_value
        _priv_raw = _priv_num.to_bytes(32, 'big')
        VAPID_PRIVATE_KEY = base64.urlsafe_b64encode(_priv_raw).rstrip(b'=').decode()
        _pub_raw = _v.public_key.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint
        )
        VAPID_PUBLIC_KEY = base64.urlsafe_b64encode(_pub_raw).rstrip(b'=').decode()
        print('=' * 70)
        print('[VAPID] Kalici bildirimler icin Railway\'e su env vars ekle:')
        print(f'  VAPID_PRIVATE_KEY={VAPID_PRIVATE_KEY}')
        print(f'  VAPID_PUBLIC_KEY={VAPID_PUBLIC_KEY}')
        print('=' * 70)
    except Exception as _ve:
        print(f'[VAPID] Anahtar olusturulamadi: {_ve}')

PUSH_ENABLED = _PUSH_LIB and bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY)

# ── Flask-Login ────────────────────────────────────────────────────────────

login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Bu sayfaya erişmek için giriş yapmalısınız.'
login_manager.login_message_category = 'warning'


class User(UserMixin):
    def __init__(self, id, kullanici_adi, email):
        self.id = id
        self.kullanici_adi = kullanici_adi
        self.email = email


@login_manager.user_loader
def load_user(user_id):
    try:
        db = get_db()
        u = db.execute('SELECT * FROM kullanicilar WHERE id=%s', (int(user_id),)).fetchone()
        if u is None:
            return None
        return User(u['id'], u['kullanici_adi'], u['email'])
    except Exception:
        return None


# ── Veritabanı bağlantısı ──────────────────────────────────────────────────

class _Conn:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        cur = self._raw.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(sql, params)
        return cur

    def commit(self):   self._raw.commit()
    def rollback(self): self._raw.rollback()
    def close(self):    self._raw.close()


def get_db():
    db = getattr(g, '_db', None)
    if db is None:
        db = g._db = _Conn(psycopg2.connect(_DB_URL))
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_db', None)
    if db is not None:
        if exception:
            db.rollback()
        db.close()


# ── Veritabanı başlatma ────────────────────────────────────────────────────

def init_db():
    db = get_db()
    for stmt in [
        """CREATE TABLE IF NOT EXISTS kullanicilar (
            id           SERIAL PRIMARY KEY,
            kullanici_adi TEXT NOT NULL UNIQUE,
            email        TEXT UNIQUE,
            sifre_hash   TEXT NOT NULL,
            created_at   TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS musteriler (
            id            SERIAL PRIMARY KEY,
            user_id       INTEGER REFERENCES kullanicilar(id) ON DELETE CASCADE,
            ad            TEXT NOT NULL,
            soyad         TEXT NOT NULL,
            telefon       TEXT,
            email         TEXT,
            butce_min     REAL,
            butce_max     REAL,
            ilgi_alanlari TEXT,
            notlar        TEXT,
            created_at    TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS ilanlar (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER REFERENCES kullanicilar(id) ON DELETE CASCADE,
            baslik      TEXT NOT NULL,
            adres       TEXT,
            sehir       TEXT,
            ilce        TEXT,
            fiyat       REAL,
            tip         TEXT DEFAULT 'satilik',
            kategori    TEXT DEFAULT 'daire',
            durum       TEXT DEFAULT 'aktif',
            metrekare   REAL,
            oda_sayisi  TEXT,
            aciklama    TEXT,
            created_at  TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS randevular (
            id          SERIAL PRIMARY KEY,
            user_id     INTEGER REFERENCES kullanicilar(id) ON DELETE CASCADE,
            musteri_id  INTEGER REFERENCES musteriler(id) ON DELETE SET NULL,
            ilan_id     INTEGER REFERENCES ilanlar(id) ON DELETE SET NULL,
            tarih       DATE NOT NULL,
            saat        TIME,
            konu        TEXT,
            notlar      TEXT,
            durum       TEXT DEFAULT 'planlandı',
            created_at  TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS musteri_ilan (
            id          SERIAL PRIMARY KEY,
            musteri_id  INTEGER NOT NULL REFERENCES musteriler(id) ON DELETE CASCADE,
            ilan_id     INTEGER NOT NULL REFERENCES ilanlar(id)    ON DELETE CASCADE,
            ilgi_durumu TEXT DEFAULT 'ilgileniyor',
            notlar      TEXT,
            created_at  TIMESTAMP DEFAULT NOW(),
            UNIQUE(musteri_id, ilan_id)
        )""",
        """CREATE TABLE IF NOT EXISTS hatirlaticlar (
            id                       SERIAL PRIMARY KEY,
            user_id                  INTEGER REFERENCES kullanicilar(id) ON DELETE CASCADE,
            baslik                   TEXT NOT NULL,
            aciklama                 TEXT,
            tarih                    DATE NOT NULL,
            saat                     TIME,
            durum                    TEXT DEFAULT 'aktif',
            bildirim_gonderildi      BOOLEAN DEFAULT FALSE,
            bildirim_1saat_gonderildi BOOLEAN DEFAULT FALSE,
            created_at               TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS push_subscriptions (
            id                SERIAL PRIMARY KEY,
            user_id           INTEGER REFERENCES kullanicilar(id) ON DELETE CASCADE,
            subscription_json TEXT NOT NULL,
            endpoint          TEXT,
            created_at        TIMESTAMP DEFAULT NOW()
        )""",
    ]:
        db.execute(stmt)

    # Var olan tablolara user_id sütunu ekle (migration)
    for alter in [
        "ALTER TABLE musteriler ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES kullanicilar(id) ON DELETE CASCADE",
        "ALTER TABLE ilanlar    ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES kullanicilar(id) ON DELETE CASCADE",
        "ALTER TABLE randevular ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES kullanicilar(id) ON DELETE CASCADE",
        "ALTER TABLE hatirlaticlar ADD COLUMN IF NOT EXISTS bildirim_1saat_gonderildi BOOLEAN DEFAULT FALSE",
    ]:
        try:
            db.execute(alter)
        except Exception:
            db.rollback()

    db.commit()


try:
    with app.app_context():
        init_db()
except Exception as _e:
    print(f'[WARNING] init_db: {_e}', flush=True)


# ── Yardımcı: sahiplik doğrulama ──────────────────────────────────────────

def own(table, id):
    """Kaydın current_user'a ait olup olmadığını döner."""
    db = get_db()
    return db.execute(f'SELECT * FROM {table} WHERE id=%s AND user_id=%s',
                      (id, current_user.id)).fetchone()


# ── Auth ───────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        kullanici_adi = request.form['kullanici_adi'].strip()
        sifre         = request.form['sifre']
        db = get_db()
        u = db.execute('SELECT * FROM kullanicilar WHERE kullanici_adi=%s',
                       (kullanici_adi,)).fetchone()
        if u and check_password_hash(u['sifre_hash'], sifre):
            login_user(User(u['id'], u['kullanici_adi'], u['email']), remember=True)
            return redirect(request.args.get('next') or url_for('index'))
        flash('Kullanıcı adı veya şifre hatalı.', 'danger')
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        kullanici_adi = request.form['kullanici_adi'].strip()
        email         = request.form.get('email', '').strip()
        sifre         = request.form['sifre']
        sifre2        = request.form['sifre2']

        if not kullanici_adi or not sifre:
            flash('Kullanıcı adı ve şifre zorunludur.', 'danger')
            return render_template('register.html')
        if sifre != sifre2:
            flash('Şifreler eşleşmiyor.', 'danger')
            return render_template('register.html')
        if len(sifre) < 6:
            flash('Şifre en az 6 karakter olmalıdır.', 'danger')
            return render_template('register.html')

        db = get_db()
        if db.execute('SELECT id FROM kullanicilar WHERE kullanici_adi=%s', (kullanici_adi,)).fetchone():
            flash('Bu kullanıcı adı zaten alınmış.', 'danger')
            return render_template('register.html')

        db.execute('INSERT INTO kullanicilar (kullanici_adi, email, sifre_hash) VALUES (%s,%s,%s)',
                   (kullanici_adi, email or None, generate_password_hash(sifre)))
        db.commit()
        flash('Hesap oluşturuldu! Giriş yapabilirsiniz.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Çıkış yapıldı.', 'info')
    return redirect(url_for('login'))


# ── PWA ────────────────────────────────────────────────────────────────────

@app.route('/sw.js')
def service_worker():
    resp = app.send_static_file('sw.js')
    resp.headers['Content-Type'] = 'application/javascript'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp


@app.route('/offline')
def offline():
    return render_template('offline.html')


# ── Dashboard ──────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    db  = get_db()
    uid = current_user.id
    stats = {
        'musteri_sayisi':   db.execute('SELECT COUNT(*) FROM musteriler WHERE user_id=%s', (uid,)).fetchone()[0],
        'ilan_sayisi':      db.execute('SELECT COUNT(*) FROM ilanlar WHERE user_id=%s', (uid,)).fetchone()[0],
        'aktif_ilan':       db.execute("SELECT COUNT(*) FROM ilanlar WHERE user_id=%s AND durum='aktif'", (uid,)).fetchone()[0],
        'yaklasan_randevu': db.execute(
            "SELECT COUNT(*) FROM randevular WHERE user_id=%s AND tarih>=CURRENT_DATE AND durum='planlandı'", (uid,)
        ).fetchone()[0],
    }
    yaklasan_randevular = db.execute('''
        SELECT r.*, m.ad||' '||m.soyad AS musteri_adi, i.baslik AS ilan_baslik
        FROM randevular r
        LEFT JOIN musteriler m ON r.musteri_id=m.id
        LEFT JOIN ilanlar    i ON r.ilan_id=i.id
        WHERE r.user_id=%s AND r.tarih>=CURRENT_DATE AND r.durum='planlandı'
        ORDER BY r.tarih, r.saat LIMIT 6
    ''', (uid,)).fetchall()
    son_musteriler = db.execute(
        'SELECT * FROM musteriler WHERE user_id=%s ORDER BY created_at DESC LIMIT 6', (uid,)
    ).fetchall()
    return render_template('index.html', stats=stats,
                           yaklasan_randevular=yaklasan_randevular,
                           son_musteriler=son_musteriler)


# ── Müşteriler ─────────────────────────────────────────────────────────────

@app.route('/musteriler')
@login_required
def musteriler():
    db  = get_db()
    uid = current_user.id
    arama = request.args.get('arama', '').strip()
    if arama:
        liste = db.execute('''
            SELECT * FROM musteriler
            WHERE user_id=%s AND (ad ILIKE %s OR soyad ILIKE %s OR telefon ILIKE %s OR email ILIKE %s)
            ORDER BY created_at DESC
        ''', (uid, *[f'%{arama}%']*4)).fetchall()
    else:
        liste = db.execute('SELECT * FROM musteriler WHERE user_id=%s ORDER BY created_at DESC', (uid,)).fetchall()
    return render_template('musteriler.html', musteriler=liste, arama=arama)


@app.route('/musteriler/ekle', methods=['GET', 'POST'])
@login_required
def musteri_ekle():
    if request.method == 'POST':
        ad    = request.form['ad'].strip()
        soyad = request.form['soyad'].strip()
        if not ad or not soyad:
            flash('Ad ve soyad zorunludur.', 'danger')
            return render_template('musteri_form.html', musteri=None, baslik='Yeni Müşteri')
        db = get_db()
        db.execute('''
            INSERT INTO musteriler (user_id,ad,soyad,telefon,email,butce_min,butce_max,ilgi_alanlari,notlar)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ''', (current_user.id, ad, soyad,
              request.form.get('telefon','').strip(),
              request.form.get('email','').strip(),
              request.form.get('butce_min') or None,
              request.form.get('butce_max') or None,
              request.form.get('ilgi_alanlari','').strip(),
              request.form.get('notlar','').strip()))
        db.commit()
        flash(f'{ad} {soyad} eklendi.', 'success')
        return redirect(url_for('musteriler'))
    return render_template('musteri_form.html', musteri=None, baslik='Yeni Müşteri')


@app.route('/musteriler/<int:id>')
@login_required
def musteri_detay(id):
    db = get_db()
    musteri = own('musteriler', id)
    if not musteri:
        flash('Kayıt bulunamadı.', 'danger')
        return redirect(url_for('musteriler'))
    randevular = db.execute('''
        SELECT r.*, i.baslik AS ilan_baslik FROM randevular r
        LEFT JOIN ilanlar i ON r.ilan_id=i.id
        WHERE r.musteri_id=%s AND r.user_id=%s ORDER BY r.tarih DESC
    ''', (id, current_user.id)).fetchall()
    ilgili_ilanlar = db.execute('''
        SELECT mi.*, i.baslik, i.adres, i.sehir, i.fiyat, i.tip, i.kategori, i.durum AS ilan_durum
        FROM musteri_ilan mi JOIN ilanlar i ON mi.ilan_id=i.id
        WHERE mi.musteri_id=%s ORDER BY mi.created_at DESC
    ''', (id,)).fetchall()
    tum_ilanlar = db.execute('''
        SELECT * FROM ilanlar
        WHERE user_id=%s AND id NOT IN (SELECT ilan_id FROM musteri_ilan WHERE musteri_id=%s)
        ORDER BY created_at DESC
    ''', (current_user.id, id)).fetchall()
    return render_template('musteri_detay.html', musteri=musteri, randevular=randevular,
                           ilgili_ilanlar=ilgili_ilanlar, tum_ilanlar=tum_ilanlar)


@app.route('/musteriler/<int:id>/duzenle', methods=['GET', 'POST'])
@login_required
def musteri_duzenle(id):
    db = get_db()
    musteri = own('musteriler', id)
    if not musteri:
        flash('Kayıt bulunamadı.', 'danger')
        return redirect(url_for('musteriler'))
    if request.method == 'POST':
        ad    = request.form['ad'].strip()
        soyad = request.form['soyad'].strip()
        if not ad or not soyad:
            flash('Ad ve soyad zorunludur.', 'danger')
            return render_template('musteri_form.html', musteri=musteri, baslik='Müşteri Düzenle')
        db.execute('''
            UPDATE musteriler SET ad=%s,soyad=%s,telefon=%s,email=%s,
            butce_min=%s,butce_max=%s,ilgi_alanlari=%s,notlar=%s WHERE id=%s AND user_id=%s
        ''', (ad, soyad,
              request.form.get('telefon','').strip(),
              request.form.get('email','').strip(),
              request.form.get('butce_min') or None,
              request.form.get('butce_max') or None,
              request.form.get('ilgi_alanlari','').strip(),
              request.form.get('notlar','').strip(),
              id, current_user.id))
        db.commit()
        flash('Güncellendi.', 'success')
        return redirect(url_for('musteri_detay', id=id))
    return render_template('musteri_form.html', musteri=musteri, baslik='Müşteri Düzenle')


@app.route('/musteriler/<int:id>/sil', methods=['POST'])
@login_required
def musteri_sil(id):
    db = get_db()
    m = own('musteriler', id)
    if m:
        db.execute('DELETE FROM musteriler WHERE id=%s AND user_id=%s', (id, current_user.id))
        db.commit()
        flash(f'{m["ad"]} {m["soyad"]} silindi.', 'success')
    return redirect(url_for('musteriler'))


@app.route('/musteriler/<int:musteri_id>/ilan-ekle', methods=['POST'])
@login_required
def musteri_ilan_ekle(musteri_id):
    if not own('musteriler', musteri_id):
        flash('Erişim izni yok.', 'danger')
        return redirect(url_for('musteriler'))
    ilan_id = request.form.get('ilan_id')
    if ilan_id:
        db = get_db()
        try:
            db.execute('INSERT INTO musteri_ilan (musteri_id,ilan_id,ilgi_durumu,notlar) VALUES (%s,%s,%s,%s)',
                       (musteri_id, ilan_id,
                        request.form.get('ilgi_durumu','ilgileniyor'),
                        request.form.get('notlar','').strip()))
            db.commit()
            flash('İlan bağlandı.', 'success')
        except psycopg2.errors.UniqueViolation:
            db.rollback()
            flash('Bu ilan zaten bağlı.', 'warning')
    return redirect(url_for('musteri_detay', id=musteri_id))


@app.route('/musteri-ilan/<int:id>/sil', methods=['POST'])
@login_required
def musteri_ilan_sil(id):
    db = get_db()
    mi = db.execute('''
        SELECT mi.* FROM musteri_ilan mi
        JOIN musteriler m ON mi.musteri_id=m.id
        WHERE mi.id=%s AND m.user_id=%s
    ''', (id, current_user.id)).fetchone()
    if mi:
        musteri_id = mi['musteri_id']
        db.execute('DELETE FROM musteri_ilan WHERE id=%s', (id,))
        db.commit()
        flash('Bağlantı kaldırıldı.', 'success')
        return redirect(url_for('musteri_detay', id=musteri_id))
    return redirect(url_for('musteriler'))


# ── İlanlar ────────────────────────────────────────────────────────────────

@app.route('/ilanlar')
@login_required
def ilanlar():
    db  = get_db()
    uid = current_user.id
    tip   = request.args.get('tip','')
    durum = request.args.get('durum','')
    arama = request.args.get('arama','').strip()
    q, p = 'SELECT * FROM ilanlar WHERE user_id=%s', [uid]
    if tip:   q += ' AND tip=%s';   p.append(tip)
    if durum: q += ' AND durum=%s'; p.append(durum)
    if arama:
        q += ' AND (baslik ILIKE %s OR adres ILIKE %s OR sehir ILIKE %s OR ilce ILIKE %s)'
        p.extend([f'%{arama}%']*4)
    q += ' ORDER BY created_at DESC'
    liste = db.execute(q, p).fetchall()
    return render_template('ilanlar.html', ilanlar=liste, tip=tip, durum=durum, arama=arama)


@app.route('/ilanlar/ekle', methods=['GET', 'POST'])
@login_required
def ilan_ekle():
    if request.method == 'POST':
        baslik = request.form['baslik'].strip()
        if not baslik:
            flash('Başlık zorunludur.', 'danger')
            return render_template('ilan_form.html', ilan=None, baslik='Yeni İlan')
        db = get_db()
        db.execute('''
            INSERT INTO ilanlar (user_id,baslik,adres,sehir,ilce,fiyat,tip,kategori,durum,metrekare,oda_sayisi,aciklama)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ''', (current_user.id, baslik,
              request.form.get('adres','').strip(),
              request.form.get('sehir','').strip(),
              request.form.get('ilce','').strip(),
              request.form.get('fiyat') or None,
              request.form.get('tip','satilik'),
              request.form.get('kategori','daire'),
              request.form.get('durum','aktif'),
              request.form.get('metrekare') or None,
              request.form.get('oda_sayisi','').strip(),
              request.form.get('aciklama','').strip()))
        db.commit()
        flash(f'"{baslik}" eklendi.', 'success')
        return redirect(url_for('ilanlar'))
    return render_template('ilan_form.html', ilan=None, baslik='Yeni İlan')


@app.route('/ilanlar/<int:id>')
@login_required
def ilan_detay(id):
    db  = get_db()
    ilan = own('ilanlar', id)
    if not ilan:
        flash('Kayıt bulunamadı.', 'danger')
        return redirect(url_for('ilanlar'))
    ilgili_musteriler = db.execute('''
        SELECT mi.*, m.ad, m.soyad, m.telefon, m.butce_min, m.butce_max
        FROM musteri_ilan mi JOIN musteriler m ON mi.musteri_id=m.id
        WHERE mi.ilan_id=%s ORDER BY mi.created_at DESC
    ''', (id,)).fetchall()
    randevular = db.execute('''
        SELECT r.*, m.ad||' '||m.soyad AS musteri_adi FROM randevular r
        LEFT JOIN musteriler m ON r.musteri_id=m.id
        WHERE r.ilan_id=%s AND r.user_id=%s ORDER BY r.tarih DESC
    ''', (id, current_user.id)).fetchall()
    return render_template('ilan_detay.html', ilan=ilan,
                           ilgili_musteriler=ilgili_musteriler, randevular=randevular)


@app.route('/ilanlar/<int:id>/duzenle', methods=['GET', 'POST'])
@login_required
def ilan_duzenle(id):
    db  = get_db()
    ilan = own('ilanlar', id)
    if not ilan:
        flash('Kayıt bulunamadı.', 'danger')
        return redirect(url_for('ilanlar'))
    if request.method == 'POST':
        baslik = request.form['baslik'].strip()
        if not baslik:
            flash('Başlık zorunludur.', 'danger')
            return render_template('ilan_form.html', ilan=ilan, baslik='İlan Düzenle')
        db.execute('''
            UPDATE ilanlar SET baslik=%s,adres=%s,sehir=%s,ilce=%s,fiyat=%s,
            tip=%s,kategori=%s,durum=%s,metrekare=%s,oda_sayisi=%s,aciklama=%s
            WHERE id=%s AND user_id=%s
        ''', (baslik,
              request.form.get('adres','').strip(),
              request.form.get('sehir','').strip(),
              request.form.get('ilce','').strip(),
              request.form.get('fiyat') or None,
              request.form.get('tip','satilik'),
              request.form.get('kategori','daire'),
              request.form.get('durum','aktif'),
              request.form.get('metrekare') or None,
              request.form.get('oda_sayisi','').strip(),
              request.form.get('aciklama','').strip(),
              id, current_user.id))
        db.commit()
        flash('Güncellendi.', 'success')
        return redirect(url_for('ilan_detay', id=id))
    return render_template('ilan_form.html', ilan=ilan, baslik='İlan Düzenle')


@app.route('/ilanlar/<int:id>/sil', methods=['POST'])
@login_required
def ilan_sil(id):
    db  = get_db()
    ilan = own('ilanlar', id)
    if ilan:
        db.execute('DELETE FROM ilanlar WHERE id=%s AND user_id=%s', (id, current_user.id))
        db.commit()
        flash(f'"{ilan["baslik"]}" silindi.', 'success')
    return redirect(url_for('ilanlar'))


# ── Randevular ─────────────────────────────────────────────────────────────

@app.route('/randevular')
@login_required
def randevular():
    db  = get_db()
    uid = current_user.id
    durum = request.args.get('durum','')
    q = '''SELECT r.*, m.ad||' '||m.soyad AS musteri_adi, i.baslik AS ilan_baslik
           FROM randevular r
           LEFT JOIN musteriler m ON r.musteri_id=m.id
           LEFT JOIN ilanlar    i ON r.ilan_id=i.id
           WHERE r.user_id=%s'''
    p = [uid]
    if durum: q += ' AND r.durum=%s'; p.append(durum)
    q += ' ORDER BY r.tarih DESC, r.saat DESC'
    liste = db.execute(q, p).fetchall()
    return render_template('randevular.html', randevular=liste, durum=durum)


@app.route('/randevular/ekle', methods=['GET', 'POST'])
@login_required
def randevu_ekle():
    db = get_db()
    if request.method == 'POST':
        tarih = request.form.get('tarih','').strip()
        if not tarih:
            flash('Tarih zorunludur.', 'danger')
            return _randevu_form(db)
        db.execute('''
            INSERT INTO randevular (user_id,musteri_id,ilan_id,tarih,saat,konu,notlar,durum)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        ''', (current_user.id,
              request.form.get('musteri_id') or None,
              request.form.get('ilan_id') or None,
              tarih,
              request.form.get('saat','').strip() or None,
              request.form.get('konu','').strip(),
              request.form.get('notlar','').strip(),
              request.form.get('durum','planlandı')))
        db.commit()
        flash('Randevu eklendi.', 'success')
        return redirect(url_for('randevular'))
    pre_m = request.args.get('musteri_id')
    pre_i = request.args.get('ilan_id')
    return _randevu_form(db, pre_musteri_id=pre_m, pre_ilan_id=pre_i)


@app.route('/randevular/<int:id>/duzenle', methods=['GET', 'POST'])
@login_required
def randevu_duzenle(id):
    db = get_db()
    randevu = own('randevular', id)
    if not randevu:
        flash('Kayıt bulunamadı.', 'danger')
        return redirect(url_for('randevular'))
    if request.method == 'POST':
        tarih = request.form.get('tarih','').strip()
        if not tarih:
            flash('Tarih zorunludur.', 'danger')
            return _randevu_form(db, randevu=randevu)
        db.execute('''
            UPDATE randevular SET musteri_id=%s,ilan_id=%s,tarih=%s,saat=%s,konu=%s,notlar=%s,durum=%s
            WHERE id=%s AND user_id=%s
        ''', (request.form.get('musteri_id') or None,
              request.form.get('ilan_id') or None,
              tarih,
              request.form.get('saat','').strip() or None,
              request.form.get('konu','').strip(),
              request.form.get('notlar','').strip(),
              request.form.get('durum','planlandı'),
              id, current_user.id))
        db.commit()
        flash('Randevu güncellendi.', 'success')
        return redirect(url_for('randevular'))
    return _randevu_form(db, randevu=randevu)


def _randevu_form(db, randevu=None, pre_musteri_id=None, pre_ilan_id=None):
    uid = current_user.id
    musteriler   = db.execute('SELECT * FROM musteriler WHERE user_id=%s ORDER BY ad,soyad', (uid,)).fetchall()
    ilan_listesi = db.execute('SELECT * FROM ilanlar WHERE user_id=%s ORDER BY baslik', (uid,)).fetchall()
    return render_template('randevu_form.html', randevu=randevu,
                           baslik='Randevu Düzenle' if randevu else 'Yeni Randevu',
                           musteriler=musteriler, ilan_listesi=ilan_listesi,
                           pre_musteri_id=pre_musteri_id, pre_ilan_id=pre_ilan_id)


@app.route('/randevular/<int:id>/sil', methods=['POST'])
@login_required
def randevu_sil(id):
    db = get_db()
    if own('randevular', id):
        db.execute('DELETE FROM randevular WHERE id=%s AND user_id=%s', (id, current_user.id))
        db.commit()
        flash('Randevu silindi.', 'success')
    return redirect(url_for('randevular'))


# ── Template Filters ───────────────────────────────────────────────────────

@app.template_filter('para')
def para_formatla(value):
    if value is None: return '—'
    try: return f"{float(value):,.0f} ₺".replace(',', '.')
    except Exception: return str(value)


@app.template_filter('tarih')
def tarih_formatla(value):
    if not value: return '—'
    try:
        if hasattr(value, 'strftime'): return value.strftime('%d.%m.%Y')
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').strftime('%d.%m.%Y')
    except Exception: return str(value)


@app.template_filter('saat')
def saat_formatla(value):
    if not value: return ''
    try:
        if hasattr(value, 'strftime'): return value.strftime('%H:%M')
        return str(value)[:5]
    except Exception: return str(value)


@app.context_processor
def inject_globals():
    badge = 0
    if current_user.is_authenticated:
        try:
            db = get_db()
            row = db.execute(
                """SELECT COUNT(*) FROM hatirlaticlar
                   WHERE user_id=%s AND durum='aktif'
                     AND tarih BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '1 day'""",
                (current_user.id,)
            ).fetchone()
            badge = row[0] if row else 0
        except Exception:
            badge = 0
    return {'hatirlatici_badge': badge, 'today': date.today(), 'tomorrow': date.today() + timedelta(days=1)}


# ── Hatırlatıcılar ────────────────────────────────────────────────────────

@app.route('/hatirlaticlar')
@login_required
def hatirlaticlar():
    db = get_db()
    uid = current_user.id
    durum = request.args.get('durum', '')
    q = 'SELECT * FROM hatirlaticlar WHERE user_id=%s'
    p = [uid]
    if durum:
        q += ' AND durum=%s'
        p.append(durum)
    q += ' ORDER BY tarih ASC, saat ASC NULLS LAST'
    liste = db.execute(q, p).fetchall()
    return render_template('hatirlaticlar.html', hatirlaticlar=liste, durum=durum)


@app.route('/hatirlaticlar/ekle', methods=['GET', 'POST'])
@login_required
def hatirlatici_ekle():
    if request.method == 'POST':
        baslik = request.form['baslik'].strip()
        tarih  = request.form.get('tarih', '').strip()
        if not baslik or not tarih:
            flash('Başlık ve tarih zorunludur.', 'danger')
            return render_template('hatirlatici_form.html', h=None, baslik='Yeni Hatırlatıcı')
        db = get_db()
        db.execute(
            '''INSERT INTO hatirlaticlar (user_id,baslik,aciklama,tarih,saat,durum)
               VALUES (%s,%s,%s,%s,%s,'aktif')''',
            (current_user.id, baslik,
             request.form.get('aciklama', '').strip() or None,
             tarih,
             request.form.get('saat', '').strip() or None)
        )
        db.commit()
        flash(f'"{baslik}" hatırlatıcısı eklendi.', 'success')
        return redirect(url_for('hatirlaticlar'))
    return render_template('hatirlatici_form.html', h=None, baslik='Yeni Hatırlatıcı')


@app.route('/hatirlaticlar/<int:id>/duzenle', methods=['GET', 'POST'])
@login_required
def hatirlatici_duzenle(id):
    db = get_db()
    h = own('hatirlaticlar', id)
    if not h:
        flash('Kayıt bulunamadı.', 'danger')
        return redirect(url_for('hatirlaticlar'))
    if request.method == 'POST':
        baslik = request.form['baslik'].strip()
        tarih  = request.form.get('tarih', '').strip()
        if not baslik or not tarih:
            flash('Başlık ve tarih zorunludur.', 'danger')
            return render_template('hatirlatici_form.html', h=h, baslik='Hatırlatıcı Düzenle')
        db.execute(
            '''UPDATE hatirlaticlar SET baslik=%s,aciklama=%s,tarih=%s,saat=%s,
               durum=%s,bildirim_gonderildi=FALSE,bildirim_1saat_gonderildi=FALSE WHERE id=%s AND user_id=%s''',
            (baslik,
             request.form.get('aciklama', '').strip() or None,
             tarih,
             request.form.get('saat', '').strip() or None,
             request.form.get('durum', 'aktif'),
             id, current_user.id)
        )
        db.commit()
        flash('Hatırlatıcı güncellendi.', 'success')
        return redirect(url_for('hatirlaticlar'))
    return render_template('hatirlatici_form.html', h=h, baslik='Hatırlatıcı Düzenle')


@app.route('/hatirlaticlar/<int:id>/tamamla', methods=['POST'])
@login_required
def hatirlatici_tamamla(id):
    db = get_db()
    if own('hatirlaticlar', id):
        db.execute("UPDATE hatirlaticlar SET durum='tamamlandi' WHERE id=%s AND user_id=%s",
                   (id, current_user.id))
        db.commit()
        flash('Tamamlandı olarak işaretlendi.', 'success')
    return redirect(url_for('hatirlaticlar'))


@app.route('/hatirlaticlar/<int:id>/sil', methods=['POST'])
@login_required
def hatirlatici_sil(id):
    db = get_db()
    h = own('hatirlaticlar', id)
    if h:
        db.execute('DELETE FROM hatirlaticlar WHERE id=%s AND user_id=%s', (id, current_user.id))
        db.commit()
        flash(f'"{h["baslik"]}" silindi.', 'success')
    return redirect(url_for('hatirlaticlar'))


# ── Push Bildirimi ─────────────────────────────────────────────────────────

@app.route('/push/vapid-public-key')
def push_vapid_key():
    return VAPID_PUBLIC_KEY, 200, {'Content-Type': 'text/plain'}


@app.route('/push/subscribe', methods=['POST'])
@login_required
def push_subscribe():
    if not PUSH_ENABLED:
        return {'error': 'Push disabled'}, 503
    data = request.get_json(silent=True)
    if not data or 'endpoint' not in data:
        return {'error': 'Invalid'}, 400
    sub_json = json.dumps(data)
    endpoint = data.get('endpoint', '')
    db = get_db()
    db.execute('DELETE FROM push_subscriptions WHERE user_id=%s AND endpoint=%s',
               (current_user.id, endpoint))
    db.execute('INSERT INTO push_subscriptions (user_id,subscription_json,endpoint) VALUES (%s,%s,%s)',
               (current_user.id, sub_json, endpoint))
    db.commit()
    return {'ok': True}, 201


@app.route('/push/unsubscribe', methods=['POST'])
@login_required
def push_unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint', '')
    if endpoint:
        db = get_db()
        db.execute('DELETE FROM push_subscriptions WHERE user_id=%s AND endpoint=%s',
                   (current_user.id, endpoint))
        db.commit()
    return {'ok': True}, 200


# ── Zamanlayıcı: 1 gün öncesi push bildirimi ──────────────────────────────

def _bildirim_gonder_job():
    try:
        conn = psycopg2.connect(_DB_URL)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT id, user_id, baslik, aciklama, saat
            FROM hatirlaticlar
            WHERE tarih = CURRENT_DATE + INTERVAL '1 day'
              AND durum = 'aktif'
              AND bildirim_gonderildi = FALSE
        """)
        reminders = cur.fetchall()
        for r in reminders:
            cur2 = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            cur2.execute('SELECT subscription_json FROM push_subscriptions WHERE user_id=%s',
                         (r['user_id'],))
            subs = cur2.fetchall()
            for sub in subs:
                try:
                    saat_str = saat_formatla(r['saat'])
                    body = r['aciklama'] or (f"Saat: {saat_str}" if saat_str else 'Yarın için hatırlatıcınız var.')
                    webpush(
                        subscription_info=json.loads(sub['subscription_json']),
                        data=json.dumps({
                            'title': f"⏰ {r['baslik']}",
                            'body': body,
                            'icon': '/static/icons/icon-192.png',
                            'badge': '/static/icons/icon-192.png',
                            'data': {'url': '/hatirlaticlar'}
                        }),
                        vapid_private_key=VAPID_PRIVATE_KEY,
                        vapid_claims={'sub': 'mailto:admin@emlakpro.com'}
                    )
                except Exception as pe:
                    print(f'[Push] Hata uid={r["user_id"]}: {pe}')
            cur.execute('UPDATE hatirlaticlar SET bildirim_gonderildi=TRUE WHERE id=%s', (r['id'],))
        conn.commit()
        cur.close()
        conn.close()
        if reminders:
            print(f'[Scheduler] {len(reminders)} hatirlatici islendi.')
    except Exception as e:
        print(f'[Scheduler] Hata: {e}')


def _bildirim_1saat_job():
    """Bugün saati olan hatırlatıcılara 1 saat kala bildirim gönderir."""
    try:
        conn = psycopg2.connect(_DB_URL)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("""
            SELECT id, user_id, baslik, aciklama, saat
            FROM hatirlaticlar
            WHERE tarih = (NOW() AT TIME ZONE 'Europe/Istanbul')::date
              AND saat IS NOT NULL
              AND saat >  ((NOW() AT TIME ZONE 'Europe/Istanbul'))::time
              AND saat <= ((NOW() AT TIME ZONE 'Europe/Istanbul') + INTERVAL '1 hour')::time
              AND durum  = 'aktif'
              AND bildirim_1saat_gonderildi = FALSE
        """)
        reminders = cur.fetchall()
        for r in reminders:
            cur2 = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            cur2.execute('SELECT subscription_json FROM push_subscriptions WHERE user_id=%s', (r['user_id'],))
            subs = cur2.fetchall()
            for sub in subs:
                try:
                    saat_str = saat_formatla(r['saat'])
                    body = r['aciklama'] or f"Saat {saat_str}'de hatırlatıcınız var."
                    webpush(
                        subscription_info=json.loads(sub['subscription_json']),
                        data=json.dumps({
                            'title': f"⏰ 1 Saat Kaldı: {r['baslik']}",
                            'body': body,
                            'icon': '/static/icons/icon-192.png',
                            'badge': '/static/icons/icon-192.png',
                            'data': {'url': '/hatirlaticlar'}
                        }),
                        vapid_private_key=VAPID_PRIVATE_KEY,
                        vapid_claims={'sub': 'mailto:admin@emlakpro.com'}
                    )
                except Exception as pe:
                    print(f'[Push-1h] Hata: {pe}')
            cur.execute('UPDATE hatirlaticlar SET bildirim_1saat_gonderildi=TRUE WHERE id=%s', (r['id'],))
        conn.commit()
        cur.close()
        conn.close()
        if reminders:
            print(f'[Scheduler-1h] {len(reminders)} hatirlatici islendi.')
    except Exception as e:
        print(f'[Scheduler-1h] Hata: {e}')


if _SCHED_AVAILABLE and PUSH_ENABLED:
    _scheduler = BackgroundScheduler(timezone='Europe/Istanbul')
    _scheduler.add_job(
        _bildirim_gonder_job,
        CronTrigger(hour=9, minute=0, timezone='Europe/Istanbul'),
        id='bildirim_ertesi',
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600
    )
    _scheduler.add_job(
        _bildirim_1saat_job,
        'interval',
        minutes=5,
        id='bildirim_1saat',
        max_instances=1,
        coalesce=True
    )
    _scheduler.start()
    atexit.register(lambda: _scheduler.shutdown(wait=False))
    print('[Scheduler] Bildirim zamanlayici basladi (09:00 ertesi gun + her 5 dk 1 saat oncesi).')


if __name__ == '__main__':
    with app.app_context():
        init_db()
    port  = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG','false').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=port)
