from flask import Flask, render_template, request, redirect, url_for, flash, g
import psycopg2
import psycopg2.extras
import psycopg2.errors
import os
from datetime import datetime

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'emlak_pro_gizli_2024')

_DB_URL = os.environ.get('DATABASE_URL', 'postgresql://localhost/emlak')
# Railway'in postgres:// prefix'ini psycopg2'nin beklediği postgresql:// ile değiştir
if _DB_URL.startswith('postgres://'):
    _DB_URL = _DB_URL.replace('postgres://', 'postgresql://', 1)


class _Conn:
    """sqlite3 benzeri arayüz sağlayan psycopg2 wrapper'ı."""
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


def init_db():
    db = get_db()
    for stmt in [
        """CREATE TABLE IF NOT EXISTS musteriler (
            id          SERIAL PRIMARY KEY,
            ad          TEXT NOT NULL,
            soyad       TEXT NOT NULL,
            telefon     TEXT,
            email       TEXT,
            butce_min   REAL,
            butce_max   REAL,
            ilgi_alanlari TEXT,
            notlar      TEXT,
            created_at  TIMESTAMP DEFAULT NOW()
        )""",
        """CREATE TABLE IF NOT EXISTS ilanlar (
            id          SERIAL PRIMARY KEY,
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
            musteri_id  INTEGER REFERENCES musteriler(id) ON DELETE SET NULL,
            ilan_id     INTEGER REFERENCES ilanlar(id)    ON DELETE SET NULL,
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
    ]:
        db.execute(stmt)
    db.commit()


# Gunicorn module import'unda ve doğrudan çalıştırmada tabloları oluştur
try:
    with app.app_context():
        init_db()
except Exception as _e:
    print(f'[WARNING] init_db: {_e}', flush=True)


# ── Dashboard ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    db = get_db()
    stats = {
        'musteri_sayisi':   db.execute('SELECT COUNT(*) FROM musteriler').fetchone()[0],
        'ilan_sayisi':      db.execute('SELECT COUNT(*) FROM ilanlar').fetchone()[0],
        'aktif_ilan':       db.execute("SELECT COUNT(*) FROM ilanlar WHERE durum='aktif'").fetchone()[0],
        'yaklasan_randevu': db.execute(
            "SELECT COUNT(*) FROM randevular WHERE tarih >= CURRENT_DATE AND durum='planlandı'"
        ).fetchone()[0],
    }
    yaklasan_randevular = db.execute('''
        SELECT r.*, m.ad || ' ' || m.soyad AS musteri_adi, i.baslik AS ilan_baslik
        FROM randevular r
        LEFT JOIN musteriler m ON r.musteri_id = m.id
        LEFT JOIN ilanlar    i ON r.ilan_id    = i.id
        WHERE r.tarih >= CURRENT_DATE AND r.durum = 'planlandı'
        ORDER BY r.tarih, r.saat
        LIMIT 6
    ''').fetchall()
    son_musteriler = db.execute(
        'SELECT * FROM musteriler ORDER BY created_at DESC LIMIT 6'
    ).fetchall()
    return render_template('index.html', stats=stats,
                           yaklasan_randevular=yaklasan_randevular,
                           son_musteriler=son_musteriler)


# ── Müşteriler ─────────────────────────────────────────────────────────────

@app.route('/musteriler')
def musteriler():
    db = get_db()
    arama = request.args.get('arama', '').strip()
    if arama:
        liste = db.execute('''
            SELECT * FROM musteriler
            WHERE ad ILIKE %s OR soyad ILIKE %s OR telefon ILIKE %s OR email ILIKE %s
            ORDER BY created_at DESC
        ''', tuple(f'%{arama}%' for _ in range(4))).fetchall()
    else:
        liste = db.execute('SELECT * FROM musteriler ORDER BY created_at DESC').fetchall()
    return render_template('musteriler.html', musteriler=liste, arama=arama)


@app.route('/musteriler/ekle', methods=['GET', 'POST'])
def musteri_ekle():
    if request.method == 'POST':
        ad = request.form['ad'].strip()
        soyad = request.form['soyad'].strip()
        if not ad or not soyad:
            flash('Ad ve soyad zorunludur.', 'danger')
            return render_template('musteri_form.html', musteri=None, baslik='Yeni Müşteri Ekle')
        db = get_db()
        db.execute('''
            INSERT INTO musteriler (ad, soyad, telefon, email, butce_min, butce_max, ilgi_alanlari, notlar)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ''', (
            ad, soyad,
            request.form.get('telefon', '').strip(),
            request.form.get('email', '').strip(),
            request.form.get('butce_min') or None,
            request.form.get('butce_max') or None,
            request.form.get('ilgi_alanlari', '').strip(),
            request.form.get('notlar', '').strip(),
        ))
        db.commit()
        flash(f'{ad} {soyad} başarıyla eklendi.', 'success')
        return redirect(url_for('musteriler'))
    return render_template('musteri_form.html', musteri=None, baslik='Yeni Müşteri Ekle')


@app.route('/musteriler/<int:id>')
def musteri_detay(id):
    db = get_db()
    musteri = db.execute('SELECT * FROM musteriler WHERE id=%s', (id,)).fetchone()
    if not musteri:
        flash('Müşteri bulunamadı.', 'danger')
        return redirect(url_for('musteriler'))
    randevular = db.execute('''
        SELECT r.*, i.baslik AS ilan_baslik
        FROM randevular r
        LEFT JOIN ilanlar i ON r.ilan_id = i.id
        WHERE r.musteri_id = %s
        ORDER BY r.tarih DESC, r.saat DESC
    ''', (id,)).fetchall()
    ilgili_ilanlar = db.execute('''
        SELECT mi.*, i.baslik, i.adres, i.sehir, i.fiyat, i.tip, i.kategori, i.durum AS ilan_durum
        FROM musteri_ilan mi
        JOIN ilanlar i ON mi.ilan_id = i.id
        WHERE mi.musteri_id = %s
        ORDER BY mi.created_at DESC
    ''', (id,)).fetchall()
    tum_ilanlar = db.execute('''
        SELECT * FROM ilanlar
        WHERE id NOT IN (SELECT ilan_id FROM musteri_ilan WHERE musteri_id=%s)
        ORDER BY created_at DESC
    ''', (id,)).fetchall()
    return render_template('musteri_detay.html', musteri=musteri, randevular=randevular,
                           ilgili_ilanlar=ilgili_ilanlar, tum_ilanlar=tum_ilanlar)


@app.route('/musteriler/<int:id>/duzenle', methods=['GET', 'POST'])
def musteri_duzenle(id):
    db = get_db()
    musteri = db.execute('SELECT * FROM musteriler WHERE id=%s', (id,)).fetchone()
    if not musteri:
        flash('Müşteri bulunamadı.', 'danger')
        return redirect(url_for('musteriler'))
    if request.method == 'POST':
        ad = request.form['ad'].strip()
        soyad = request.form['soyad'].strip()
        if not ad or not soyad:
            flash('Ad ve soyad zorunludur.', 'danger')
            return render_template('musteri_form.html', musteri=musteri, baslik='Müşteri Düzenle')
        db.execute('''
            UPDATE musteriler SET ad=%s, soyad=%s, telefon=%s, email=%s,
            butce_min=%s, butce_max=%s, ilgi_alanlari=%s, notlar=%s
            WHERE id=%s
        ''', (
            ad, soyad,
            request.form.get('telefon', '').strip(),
            request.form.get('email', '').strip(),
            request.form.get('butce_min') or None,
            request.form.get('butce_max') or None,
            request.form.get('ilgi_alanlari', '').strip(),
            request.form.get('notlar', '').strip(),
            id,
        ))
        db.commit()
        flash(f'{ad} {soyad} güncellendi.', 'success')
        return redirect(url_for('musteri_detay', id=id))
    return render_template('musteri_form.html', musteri=musteri, baslik='Müşteri Düzenle')


@app.route('/musteriler/<int:id>/sil', methods=['POST'])
def musteri_sil(id):
    db = get_db()
    musteri = db.execute('SELECT * FROM musteriler WHERE id=%s', (id,)).fetchone()
    if musteri:
        db.execute('DELETE FROM musteriler WHERE id=%s', (id,))
        db.commit()
        flash(f'{musteri["ad"]} {musteri["soyad"]} silindi.', 'success')
    return redirect(url_for('musteriler'))


@app.route('/musteriler/<int:musteri_id>/ilan-ekle', methods=['POST'])
def musteri_ilan_ekle(musteri_id):
    ilan_id = request.form.get('ilan_id')
    if ilan_id:
        db = get_db()
        try:
            db.execute('''
                INSERT INTO musteri_ilan (musteri_id, ilan_id, ilgi_durumu, notlar)
                VALUES (%s, %s, %s, %s)
            ''', (musteri_id, ilan_id,
                  request.form.get('ilgi_durumu', 'ilgileniyor'),
                  request.form.get('notlar', '').strip()))
            db.commit()
            flash('İlan müşteriye bağlandı.', 'success')
        except psycopg2.errors.UniqueViolation:
            db.rollback()
            flash('Bu ilan zaten müşteriye bağlı.', 'warning')
    return redirect(url_for('musteri_detay', id=musteri_id))


@app.route('/musteri-ilan/<int:id>/sil', methods=['POST'])
def musteri_ilan_sil(id):
    db = get_db()
    mi = db.execute('SELECT * FROM musteri_ilan WHERE id=%s', (id,)).fetchone()
    if mi:
        musteri_id = mi['musteri_id']
        db.execute('DELETE FROM musteri_ilan WHERE id=%s', (id,))
        db.commit()
        flash('İlan bağlantısı kaldırıldı.', 'success')
        return redirect(url_for('musteri_detay', id=musteri_id))
    return redirect(url_for('musteriler'))


# ── İlanlar ────────────────────────────────────────────────────────────────

@app.route('/ilanlar')
def ilanlar():
    db = get_db()
    tip   = request.args.get('tip', '')
    durum = request.args.get('durum', '')
    arama = request.args.get('arama', '').strip()
    query  = 'SELECT * FROM ilanlar WHERE 1=1'
    params = []
    if tip:
        query += ' AND tip=%s';   params.append(tip)
    if durum:
        query += ' AND durum=%s'; params.append(durum)
    if arama:
        query += ' AND (baslik ILIKE %s OR adres ILIKE %s OR sehir ILIKE %s OR ilce ILIKE %s)'
        params.extend([f'%{arama}%'] * 4)
    query += ' ORDER BY created_at DESC'
    liste = db.execute(query, params).fetchall()
    return render_template('ilanlar.html', ilanlar=liste, tip=tip, durum=durum, arama=arama)


@app.route('/ilanlar/ekle', methods=['GET', 'POST'])
def ilan_ekle():
    if request.method == 'POST':
        baslik = request.form['baslik'].strip()
        if not baslik:
            flash('Başlık zorunludur.', 'danger')
            return render_template('ilan_form.html', ilan=None, baslik='Yeni İlan Ekle')
        db = get_db()
        db.execute('''
            INSERT INTO ilanlar
              (baslik, adres, sehir, ilce, fiyat, tip, kategori, durum, metrekare, oda_sayisi, aciklama)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ''', (
            baslik,
            request.form.get('adres', '').strip(),
            request.form.get('sehir', '').strip(),
            request.form.get('ilce', '').strip(),
            request.form.get('fiyat') or None,
            request.form.get('tip', 'satilik'),
            request.form.get('kategori', 'daire'),
            request.form.get('durum', 'aktif'),
            request.form.get('metrekare') or None,
            request.form.get('oda_sayisi', '').strip(),
            request.form.get('aciklama', '').strip(),
        ))
        db.commit()
        flash(f'"{baslik}" ilanı eklendi.', 'success')
        return redirect(url_for('ilanlar'))
    return render_template('ilan_form.html', ilan=None, baslik='Yeni İlan Ekle')


@app.route('/ilanlar/<int:id>')
def ilan_detay(id):
    db = get_db()
    ilan = db.execute('SELECT * FROM ilanlar WHERE id=%s', (id,)).fetchone()
    if not ilan:
        flash('İlan bulunamadı.', 'danger')
        return redirect(url_for('ilanlar'))
    ilgili_musteriler = db.execute('''
        SELECT mi.*, m.ad, m.soyad, m.telefon, m.butce_min, m.butce_max
        FROM musteri_ilan mi
        JOIN musteriler m ON mi.musteri_id = m.id
        WHERE mi.ilan_id = %s
        ORDER BY mi.created_at DESC
    ''', (id,)).fetchall()
    randevular = db.execute('''
        SELECT r.*, m.ad || ' ' || m.soyad AS musteri_adi
        FROM randevular r
        LEFT JOIN musteriler m ON r.musteri_id = m.id
        WHERE r.ilan_id = %s
        ORDER BY r.tarih DESC
    ''', (id,)).fetchall()
    return render_template('ilan_detay.html', ilan=ilan,
                           ilgili_musteriler=ilgili_musteriler,
                           randevular=randevular)


@app.route('/ilanlar/<int:id>/duzenle', methods=['GET', 'POST'])
def ilan_duzenle(id):
    db = get_db()
    ilan = db.execute('SELECT * FROM ilanlar WHERE id=%s', (id,)).fetchone()
    if not ilan:
        flash('İlan bulunamadı.', 'danger')
        return redirect(url_for('ilanlar'))
    if request.method == 'POST':
        baslik = request.form['baslik'].strip()
        if not baslik:
            flash('Başlık zorunludur.', 'danger')
            return render_template('ilan_form.html', ilan=ilan, baslik='İlan Düzenle')
        db.execute('''
            UPDATE ilanlar SET baslik=%s, adres=%s, sehir=%s, ilce=%s, fiyat=%s,
            tip=%s, kategori=%s, durum=%s, metrekare=%s, oda_sayisi=%s, aciklama=%s
            WHERE id=%s
        ''', (
            baslik,
            request.form.get('adres', '').strip(),
            request.form.get('sehir', '').strip(),
            request.form.get('ilce', '').strip(),
            request.form.get('fiyat') or None,
            request.form.get('tip', 'satilik'),
            request.form.get('kategori', 'daire'),
            request.form.get('durum', 'aktif'),
            request.form.get('metrekare') or None,
            request.form.get('oda_sayisi', '').strip(),
            request.form.get('aciklama', '').strip(),
            id,
        ))
        db.commit()
        flash(f'"{baslik}" güncellendi.', 'success')
        return redirect(url_for('ilan_detay', id=id))
    return render_template('ilan_form.html', ilan=ilan, baslik='İlan Düzenle')


@app.route('/ilanlar/<int:id>/sil', methods=['POST'])
def ilan_sil(id):
    db = get_db()
    ilan = db.execute('SELECT * FROM ilanlar WHERE id=%s', (id,)).fetchone()
    if ilan:
        db.execute('DELETE FROM ilanlar WHERE id=%s', (id,))
        db.commit()
        flash(f'"{ilan["baslik"]}" ilanı silindi.', 'success')
    return redirect(url_for('ilanlar'))


# ── Randevular ─────────────────────────────────────────────────────────────

@app.route('/randevular')
def randevular():
    db = get_db()
    durum  = request.args.get('durum', '')
    query  = '''
        SELECT r.*, m.ad || ' ' || m.soyad AS musteri_adi, i.baslik AS ilan_baslik
        FROM randevular r
        LEFT JOIN musteriler m ON r.musteri_id = m.id
        LEFT JOIN ilanlar    i ON r.ilan_id    = i.id
        WHERE 1=1
    '''
    params = []
    if durum:
        query += ' AND r.durum=%s'; params.append(durum)
    query += ' ORDER BY r.tarih DESC, r.saat DESC'
    liste = db.execute(query, params).fetchall()
    return render_template('randevular.html', randevular=liste, durum=durum)


@app.route('/randevular/ekle', methods=['GET', 'POST'])
def randevu_ekle():
    db = get_db()
    if request.method == 'POST':
        tarih = request.form.get('tarih', '').strip()
        if not tarih:
            flash('Tarih zorunludur.', 'danger')
            return _randevu_form(db, None, 'Yeni Randevu')
        db.execute('''
            INSERT INTO randevular (musteri_id, ilan_id, tarih, saat, konu, notlar, durum)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (
            request.form.get('musteri_id') or None,
            request.form.get('ilan_id') or None,
            tarih,
            request.form.get('saat', '').strip() or None,
            request.form.get('konu', '').strip(),
            request.form.get('notlar', '').strip(),
            request.form.get('durum', 'planlandı'),
        ))
        db.commit()
        flash('Randevu eklendi.', 'success')
        return redirect(url_for('randevular'))
    pre_musteri = request.args.get('musteri_id')
    pre_ilan    = request.args.get('ilan_id')
    return _randevu_form(db, None, 'Yeni Randevu',
                         pre_musteri_id=pre_musteri, pre_ilan_id=pre_ilan)


@app.route('/randevular/<int:id>/duzenle', methods=['GET', 'POST'])
def randevu_duzenle(id):
    db = get_db()
    randevu = db.execute('SELECT * FROM randevular WHERE id=%s', (id,)).fetchone()
    if not randevu:
        flash('Randevu bulunamadı.', 'danger')
        return redirect(url_for('randevular'))
    if request.method == 'POST':
        tarih = request.form.get('tarih', '').strip()
        if not tarih:
            flash('Tarih zorunludur.', 'danger')
            return _randevu_form(db, randevu, 'Randevu Düzenle')
        db.execute('''
            UPDATE randevular
            SET musteri_id=%s, ilan_id=%s, tarih=%s, saat=%s, konu=%s, notlar=%s, durum=%s
            WHERE id=%s
        ''', (
            request.form.get('musteri_id') or None,
            request.form.get('ilan_id') or None,
            tarih,
            request.form.get('saat', '').strip() or None,
            request.form.get('konu', '').strip(),
            request.form.get('notlar', '').strip(),
            request.form.get('durum', 'planlandı'),
            id,
        ))
        db.commit()
        flash('Randevu güncellendi.', 'success')
        return redirect(url_for('randevular'))
    return _randevu_form(db, randevu, 'Randevu Düzenle')


def _randevu_form(db, randevu, baslik, pre_musteri_id=None, pre_ilan_id=None):
    musteriler   = db.execute('SELECT * FROM musteriler ORDER BY ad, soyad').fetchall()
    ilan_listesi = db.execute('SELECT * FROM ilanlar ORDER BY baslik').fetchall()
    return render_template('randevu_form.html', randevu=randevu, baslik=baslik,
                           musteriler=musteriler, ilan_listesi=ilan_listesi,
                           pre_musteri_id=pre_musteri_id, pre_ilan_id=pre_ilan_id)


@app.route('/randevular/<int:id>/sil', methods=['POST'])
def randevu_sil(id):
    db = get_db()
    db.execute('DELETE FROM randevular WHERE id=%s', (id,))
    db.commit()
    flash('Randevu silindi.', 'success')
    return redirect(url_for('randevular'))


# ── Template Filters ───────────────────────────────────────────────────────

@app.template_filter('para')
def para_formatla(value):
    if value is None:
        return '—'
    try:
        return f"{float(value):,.0f} ₺".replace(',', '.')
    except Exception:
        return str(value)


@app.template_filter('tarih')
def tarih_formatla(value):
    if not value:
        return '—'
    try:
        # PostgreSQL date/datetime nesneleri doğrudan strftime destekler
        if hasattr(value, 'strftime'):
            return value.strftime('%d.%m.%Y')
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').strftime('%d.%m.%Y')
    except Exception:
        return str(value)


if __name__ == '__main__':
    with app.app_context():
        init_db()
    port  = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug, host='0.0.0.0', port=port)
