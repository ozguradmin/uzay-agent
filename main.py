from flask import Flask, jsonify, request, render_template
from dotenv import load_dotenv
import os
import requests
import base64
from flask import request
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import re
from PIL import Image
from io import BytesIO
from google.oauth2 import service_account
from google.auth.transport.requests import Request
import google.auth
import google.auth.transport.requests
from apscheduler.schedulers.background import BackgroundScheduler
import atexit
import random
import time
import logging

# .env dosyasındaki ortam değişkenlerini yükle
load_dotenv()

# logging'i ayarla
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)

# Zamanlayıcıyı global olarak tanımla
scheduler = BackgroundScheduler(daemon=True)

@app.route('/')
def admin_panel():
    """Admin panelinin ana sayfasını gösterir."""
    return render_template('index.html')

@app.route('/api/status', methods=['GET'])
def api_status():
    """
    API'nin ve temel yapılandırmanın durumunu kontrol etmek için bir endpoint.
    """
    return jsonify({
        "status": "ok",
        "message": "Agent API başarıyla çalışıyor."
    })

@app.route('/test-wordpress', methods=['POST'])
def test_wordpress_connection():
    """
    WordPress sitesine bir test gönderisi (taslak olarak) yollayarak
    bağlantıyı ve kimlik bilgilerini doğrular.
    """
    wp_url = os.getenv("WORDPRESS_URL")
    wp_user = os.getenv("WORDPRESS_USER")
    wp_password = os.getenv("WORDPRESS_APP_PASSWORD")

    if not all([wp_url, wp_user, wp_password]):
        return jsonify({"error": "WordPress bilgileri .env dosyasında eksik."}), 400

    api_url = f"{wp_url.rstrip('/')}/wp-json/wp/v2/posts"

    # WordPress Uygulama Şifresi için Basic Auth kullanıyoruz.
    credentials = f"{wp_user}:{wp_password}"
    token = base64.b64encode(credentials.encode())

    headers = {
        'Authorization': f'Basic {token.decode("utf-8")}',
        'Content-Type': 'application/json'
    }

    post_data = {
        "title": "UzayAgent Test Yazısı",
        "content": "Bu yazı, UzayAgent'ın WordPress sitenizle başarılı bir şekilde iletişim kurduğunu doğrulamak için otomatik olarak oluşturulmuştur.",
        "status": "draft"  # Yazıyı taslak olarak kaydet
    }

    try:
        response = requests.post(api_url, headers=headers, json=post_data, timeout=20)
        response.raise_for_status()  # HTTP 2xx dışında bir durum kodu varsa hata fırlatır.

        return jsonify({
            "status": "success",
            "message": "WordPress'e test yazısı başarıyla gönderildi! Lütfen taslaklarınızı kontrol edin.",
            "post_details": response.json()
        }), 201

    except requests.exceptions.RequestException as e:
        return jsonify({
            "status": "error",
            "message": f"WordPress'e bağlanırken bir hata oluştu: {e}"
        }), 500

def search_google(query: str, num_results: int = 5, time_filter: str = None):
    """
    Google Custom Search API kullanarak bir arama sorgusu gerçekleştirir.
    time_filter: "qdr:d" (son 24 saat), "qdr:w" (son hafta), "qdr:m" (son ay)
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    search_engine_id = os.getenv("GOOGLE_SEARCH_ENGINE_ID")

    if not api_key or not search_engine_id:
        raise ValueError("Google API anahtarı veya Arama Motoru Kimliği eksik.")

    try:
        service = build("customsearch", "v1", developerKey=api_key)
        
        # Arama parametreleri
        search_params = {
            'q': query,
            'cx': search_engine_id,
            'num': num_results,
            'lr': 'lang_en',  # İngilizce sonuçlar için
            'gl': 'us'  # ABD'den sonuçlar
        }
        
        # Zaman filtresi ekle
        if time_filter:
            search_params['dateRestrict'] = time_filter
            
        result = service.cse().list(**search_params).execute()
        return result.get('items', [])
    except Exception as e:
        logging.error(f"Google araması sırasında bir hata oluştu: {e}")
        return []

@app.route('/search', methods=['POST'])
def handle_search():
    """
    Bir arama sorgusu alır, Google'da arar ve sonuçları döndürür.
    JSON Body: {"query": "aranacak konu"}
    """
    data = request.json
    query = data.get('query')

    if not query:
        return jsonify({"error": "Lütfen bir arama sorgusu belirtin."}), 400

    try:
        search_results = search_google(query)
        # Sadece başlık ve link bilgilerini alalım
        simplified_results = [{"title": item.get('title'), "link": item.get('link')} for item in search_results]

        return jsonify({
            "status": "success",
            "query": query,
            "results": simplified_results
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Beklenmedik bir hata oluştu: {e}"}), 500


def generate_content_with_gemini(prompt: str):
    """
    Verilen bir prompt ile Gemini API'sine doğrudan istek göndererek bir metin içeriği oluşturur.
    """
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        raise ValueError("Gemini API anahtarı .env dosyasında eksik.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent?key={gemini_api_key}"

    headers = {
        'Content-Type': 'application/json'
    }

    data = {
        "contents": [{
            "parts": [{
                "text": prompt
            }]
        }]
    }

    try:
        response = requests.post(url, headers=headers, json=data, timeout=60)
        response.raise_for_status()
        
        candidates = response.json().get('candidates', [])
        if candidates and candidates[0].get('content', {}).get('parts', []):
            return candidates[0]['content']['parts'][0]['text']
        else:
            raise ValueError("Gemini API'sinden geçerli bir yanıt alınamadı.")
            
    except requests.exceptions.RequestException as e:
        logging.error(f"Gemini API'sine istek gönderirken hata oluştu: {e}")
        logging.error(f"Yanıt içeriği: {e.response.text if e.response else 'Yanıt yok'}")
        raise
    except Exception as e:
        logging.error(f"Gemini ile içerik üretirken hata oluştu: {e}")
        raise

def generate_ai_image(prompt: str):
    """
    Gemini 2.5 Flash Image Preview modeli kullanarak AI görseli üretir.
    """
    logging.info(f"AI görseli üretiliyor: '{prompt[:70]}...'")
    
    try:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            logging.error("!!! HATA: GEMINI_API_KEY bulunamadı")
            return None

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-image-preview:generateContent?key={api_key}"
        
        headers = {
            'Content-Type': 'application/json'
        }
        
        data = {
            "contents": [{
                "parts": [
                    {"text": prompt}
                ]
            }]
        }

        response = requests.post(url, headers=headers, json=data, timeout=120)
        response.raise_for_status()
        
        response_data = response.json()
        
        # Gelen base64 verisini decode et
        if 'candidates' in response_data and len(response_data['candidates']) > 0:
            candidate = response_data['candidates'][0]
            if 'content' in candidate and 'parts' in candidate['content']:
                for part in candidate['content']['parts']:
                    if 'inlineData' in part and 'data' in part['inlineData']:
                        image_bytes = base64.b64decode(part['inlineData']['data'])
                        logging.info("AI görseli başarıyla üretildi.")
                        return image_bytes
        
        logging.error("!!! HATA: AI görseli üretilemedi - yanıt formatı beklenmeyen")
        return None

    except Exception as e:
        logging.error(f"!!! HATA: AI görseli üretilemedi: {e}")
        return None

def markdown_to_html_links(text):
    """Metin içindeki Markdown link formatını [text](url) HTML <a> etiketine dönüştürür."""
    return re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2">\1</a>', text)

def build_wordpress_content(title, main_content_parts, nasa_image_url, nasa_image_id, ai_image_url, ai_image_id, sources, ai_image2_url=None, ai_image2_id=None):
    """
    Verilen yapılandırılmış veri parçalarından WordPress blok düzenleyici formatında tam bir HTML içeriği oluşturur.
    """
    
    # 1. Yazı Başlığı Bloğu
    content_html = f'<!-- wp:quote -->\n<blockquote class="wp-block-quote"><!-- wp:heading {{"level":3}} -->\n<h3 class="wp-block-heading"><strong>{title}</strong></h3>\n<!-- /wp:heading --></blockquote>\n<!-- /wp:quote -->\n\n'
    
    # 2. NASA Görseli (varsa)
    if nasa_image_url and nasa_image_id:
        nasa_image_block = f'<!-- wp:image {{"id":{nasa_image_id},"sizeSlug":"large","style":{{"border":{{"radius":"10px"}}}}}} -->\n<figure class="wp-block-image size-large has-custom-border"><img src="{nasa_image_url}" alt="{title}" class="wp-image-{nasa_image_id}" style="border-radius:10px"/></figure>\n<!-- /wp:image -->\n\n'
        content_html += nasa_image_block

    # 3. Ana İçerik Blokları ve AI Görselleri
    ai_image1_inserted = False
    ai_image2_inserted = False
    h2_count = 0
    temp_content = ""
    for part_type, part_content in main_content_parts:
        # Alt başlıklar <h2> olmalı, <p> içinde değil.
        if part_type == 'h2':
            h2_count += 1
            temp_content += f'<!-- wp:heading -->\n<h2 class="wp-block-heading">{part_content}</h2>\n<!-- /wp:heading -->\n\n'
            # 2. H2'den sonra 1. AI görselini ekle
            if not ai_image1_inserted and ai_image_url and ai_image_id and h2_count >= 2:
                ai_image1_block = f'<!-- wp:image {{"id":{ai_image_id},"sizeSlug":"large","style":{{"border":{{"radius":"10px"}}}}}} -->\n<figure class="wp-block-image size-large has-custom-border"><img src="{ai_image_url}" alt="{title} - Yapay Zeka Görseli 1" class="wp-image-{ai_image_id}" style="border-radius:10px"/></figure>\n<!-- /wp:image -->\n\n'
                temp_content += ai_image1_block
                ai_image1_inserted = True
            # 3. H2'den sonra 2. AI görselini ekle
            elif not ai_image2_inserted and ai_image2_url and ai_image2_id and h2_count >= 3:
                ai_image2_block = f'<!-- wp:image {{"id":{ai_image2_id},"sizeSlug":"large","style":{{"border":{{"radius":"10px"}}}}}} -->\n<figure class="wp-block-image size-large has-custom-border"><img src="{ai_image2_url}" alt="{title} - Yapay Zeka Görseli 2" class="wp-image-{ai_image2_id}" style="border-radius:10px"/></figure>\n<!-- /wp:image -->\n\n'
                temp_content += ai_image2_block
                ai_image2_inserted = True
        elif part_type == 'p':
            processed_paragraph = markdown_to_html_links(part_content)
            temp_content += f'<!-- wp:paragraph -->\n<p>{processed_paragraph}</p>\n<!-- /wp:paragraph -->\n\n'
    
    content_html += temp_content
            
    # 4. Kaynaklar Bloğu
    if sources:
        sources_html = "<!-- wp:list -->\n<ul>"
        for source in sources:
            match = re.match(r'\[(.*?)\]\((.*?)\)', source)
            if match:
                name, url = match.groups()
                sources_html += f'<!-- wp:list-item -->\n<li><a href="{url}">{name}</a></li>\n<!-- /wp:list-item -->'
        sources_html += "</ul>\n<!-- /wp:list -->"
        
        details_json = '{"style":{"elements":{"link":{"color":{"text":"#888888"}}},"color":{"text":"#888888"}}}'
        content_html += f'\n<!-- wp:details {details_json} -->\n<details class="wp-block-details has-text-color has-link-color" style="color:#888888"><summary>Kaynaklar</summary>{sources_html}</details>\n<!-- /wp:details -->\n'
        
    return content_html


def upload_image_to_wordpress(title: str, image_url: str = None, image_data: bytes = None):
    """
    Verilen URL'den veya doğrudan byte verisinden bir görseli alır, 
    optimize eder ve WordPress medya kütüphanesine yükler.
    Yüklenen medyanın ID'sini ve URL'sini içeren bir dictionary döndürür.
    """
    wp_url = os.getenv("WORDPRESS_URL")
    wp_user = os.getenv("WORDPRESS_USER")
    wp_password = os.getenv("WORDPRESS_APP_PASSWORD")
    
    image_content = None
    image_name = "ai_generated_image.jpg"

    if image_data:
        image_content = image_data
    elif image_url:
        try:
            image_response = requests.get(image_url, stream=True, timeout=30)
            image_response.raise_for_status()
            image_name = image_url.split("/")[-1]
            image_content = image_response.content
        except requests.exceptions.RequestException as e:
            logging.error(f"Görsel indirilirken hata: {e}")
            return None
    
    if not image_content:
        logging.error("Yüklenecek görsel verisi bulunamadı.")
        return None

    # Pillow ile görsel optimizasyonu
    try:
        img = Image.open(BytesIO(image_content))
        
        # Genişliği 1200px'den büyükse yeniden boyutlandır
        if img.width > 1200:
            ratio = 1200 / img.width
            new_height = int(img.height * ratio)
            img = img.resize((1200, new_height), Image.Resampling.LANCZOS)
            
        # Optimize edilmiş görseli bir byte stream'e kaydet
        output_buffer = BytesIO()
        img.save(output_buffer, format='JPEG', quality=85, optimize=True)
        optimized_image_data = output_buffer.getvalue()

    except Exception as e:
        logging.error(f"Görsel işlenirken hata (Pillow): {e}")
        return None

    # WordPress'e yükle
    media_api_url = f"{wp_url.rstrip('/')}/wp-json/wp/v2/media"
    credentials = f"{wp_user}:{wp_password}"
    token = base64.b64encode(credentials.encode())
    headers = {
        'Authorization': f'Basic {token.decode("utf-8")}',
        'Content-Disposition': f'attachment; filename="{image_name}"'
    }

    try:
        upload_response = requests.post(media_api_url, headers=headers, data=optimized_image_data, timeout=60)
        upload_response.raise_for_status()
        media_data = upload_response.json()
        logging.info(f"Görsel başarıyla yüklendi. Media ID: {media_data['id']}")
        return {
            "id": media_data['id'],
            "url": media_data['source_url']
        }
    except requests.exceptions.RequestException as e:
        logging.error(f"Görsel WordPress'e yüklenirken hata: {e}")
        logging.error(f"Hata detayı: {e.response.text if e.response else 'Yanıt yok'}")
        return None

def get_nasa_apod():
    """
    NASA APOD (Astronomy Picture of the Day) API'sinden günün görselini ve bilgilerini çeker.
    """
    nasa_api_key = os.getenv("NASA_API_KEY")
    if not nasa_api_key:
        raise ValueError("NASA API anahtarı .env dosyasında eksik.")
        
    url = f"https://api.nasa.gov/planetary/apod?api_key={nasa_api_key}"
    
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"NASA APOD API'sine bağlanırken hata oluştu: {e}")
        return None

def post_to_wordpress(title: str, content: str, featured_media_id: int = None, meta_description: str = None, schedule_time: str = None):
    """
    Verilen başlık ve içerikle WordPress'e bir yazı gönderir.
    Eğer schedule_time verilirse, gönderiyi o tarihe zamanlar.
    """
    wp_url = os.getenv("WORDPRESS_URL")
    wp_user = os.getenv("WORDPRESS_USER")
    wp_password = os.getenv("WORDPRESS_APP_PASSWORD")

    if not all([wp_url, wp_user, wp_password]):
        raise ValueError("WordPress bilgileri .env dosyasında eksik.")

    api_url = f"{wp_url.rstrip('/')}/wp-json/wp/v2/posts"
    credentials = f"{wp_user}:{wp_password}"
    token = base64.b64encode(credentials.encode())
    headers = {
        'Authorization': f'Basic {token.decode("utf-8")}',
        'Content-Type': 'application/json'
    }
    post_data = {
        "title": title,
        "content": content,
        "status": "draft" # Varsayılan olarak taslak olarak gönder
    }
    
    if featured_media_id:
        post_data["featured_media"] = featured_media_id
    
    if meta_description:
        post_data["meta"] = {
            "_yoast_wpseo_metadesc": meta_description
        }

    if schedule_time:
        post_data['status'] = 'future'
        post_data['date'] = schedule_time

    response = requests.post(api_url, headers=headers, json=post_data, timeout=30)
    response.raise_for_status()
    return response.json()


def get_smart_schedule_times():
    """
    O gün için 4 adet akıllı ve rastgele zamanlanmış yayın saati üretir.
    WordPress API'sinin beklediği ISO 8601 formatında döndürür.
    """
    now = datetime.now()
    schedule_times = []

    # Zaman aralıkları (saat olarak) ve aralıklar (dakika olarak)
    time_slots = {
        "morning": (8, 30, 9, 0),    # 08:30 - 09:00
        "noon": (12, 30, 13, 0),   # 12:30 - 13:00
        "afternoon": (17, 30, 18, 0), # 17:30 - 18:00
        "evening": (21, 0, 21, 30)    # 21:00 - 21:30
    }

    for key, (start_h, start_m, end_h, end_m) in time_slots.items():
        # Başlangıç ve bitiş zamanlarını oluştur
        start_time = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        end_time = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)

        # Başlangıç ve bitiş zamanları arasındaki toplam saniye sayısını hesapla
        time_difference = (end_time - start_time).total_seconds()

        # Rastgele bir saniye değeri seç
        random_seconds = random.randint(0, int(time_difference))

        # Rastgele zamanı hesapla
        scheduled_time = start_time + timedelta(seconds=random_seconds)
        
        # WordPress'in anlayacağı ISO 8601 formatına çevir (T ile)
        schedule_times.append(scheduled_time.isoformat())

    return schedule_times


def generate_and_post_logic(topic: str, schedule_time: str = None):
    """
    `generate_and_post` endpoint'inin ana mantığını içerir. Artık jsonify DÖNDÜRMÜYOR.
    """
    if not topic:
        logging.error("HATA: Konu belirtilmedi.")
        return

    try:
        # 1. Adım: Konuyu İngilizce'ye çevir
        logging.info(f"'{topic}' konusu İngilizce'ye çevriliyor...")
        translation_prompt = f"Aşağıdaki Türkçe astronomi haber başlığını, Google'da en iyi sonuçları bulacak şekilde etkili bir İngilizce arama sorgusuna çevir. Sadece çevrilmiş sorguyu döndür, başka bir şey yazma.\n\nTÜRKÇE BAŞLIK: {topic}"
        english_query = generate_content_with_gemini(translation_prompt).strip()
        logging.info(f"İngilizce sorgu oluşturuldu: '{english_query}'")

        # 2. Adım: Araştırma (önce son 24 saat, sonra son 1 hafta)
        logging.info(f"'{english_query}' sorgusu için son 24 saatte araştırma yapılıyor...")
        search_results = search_google(english_query, time_filter="qdr:d")
        
        if not search_results:
            logging.info(f"'{english_query}' için son 24 saatte sonuç bulunamadı, son 1 hafta deneniyor...")
            search_results = search_google(english_query, time_filter="qdr:w")

        if not search_results:
            logging.info(f"'{english_query}' için son 1 haftada sonuç bulunamadı, konu atlanıyor.")
            return jsonify({
                "status": "skipped",
                "message": "Konu güncel değil veya son 1 haftada kaynak bulunamadı."
            }), 200

        simplified_results = [{"title": item.get('title'), "link": item.get('link')} for item in search_results]
        sources_text = "\n".join([f"- {result['title']}: {result['link']}" for result in simplified_results])

        # 3. Adım: İçerik Üretme
        logging.info("Arama sonuçları Gemini'ye gönderiliyor ve içerik üretiliyor...")

        prompt = f"""
        Sen, galaktikuzay.com için yazan, Neil deGrasse Tyson gibi karmaşık konuları basit ve heyecan verici bir dille anlatan bir bilim iletişimcisisin. Görevin, verilen konuyu analiz edip, SEO uyumlu, yapılandırılmış bir blog yazısı verisi oluşturmak.

        **KONU:** {topic}
        
        **KAYNAKLAR:**
        {sources_text}

        **KESİN KURALLAR:**
        1.  **SEO Başlığı:** 65 karakteri geçmeyen, merak uyandıran, spesifik ve ilgi çekici bir Türkçe başlık üret. 
            Örnekler: "Mars'ta Yaşam İzi Bulundu!", "Jüpiter'in Büyük Kırmızı Lekesi Küçülüyor", "Uzayda Su Bulundu!", "Kara Delik Yıldızı Yuttu", "Ay'dan İlk Örnekler Geldi"
            Genel ifadeler kullanma: "NASA Açıkladı", "Bilim Haberi", "Harika bir görev", "Kozmik" gibi.
            Başlık doğrudan konuyu anlatsın, yapay zeka hissi vermesin.
        2.  **Meta Açıklama:** 160 karakteri geçmeyen, anahtar kelimeleri içeren bir meta açıklama yaz.
        3.  **Yazı Başlığı (H3):** İçerikte gösterilecek, daha sanatsal ve uzun bir başlık üret.
        4.  **İçerik Akışı:**
            *   YUKARIDAKİ KONUYU temel alarak, en az 400 kelimelik özgün bir metin oluştur.
            *   Metni `[H2]` etiketleriyle mantıksal alt başlıklara ayır.
            *   Her alt başlığın altına `[P]` etiketleriyle paragraflar ekle.
            *   Metin içinde `[Site Adı](URL)` gibi en az iki adet Markdown formatında dış link ver.
            *   Metne kişisel bir dokunuş kat.
            *   NASA APOD verilerini kullanma - sadece verilen konuya odaklan.
        5.  **Kaynaklar:** Link verdiğin kaynakları "Kaynaklar" bölümü için listele. SADECE YUKARIDA VERİLEN KAYNAKLARI KULLAN. ASLA SOSYAL MEDYA (INSTAGRAM, TWITTER VB.) LİNKİ VERME.
        
        **ÇIKTI FORMATI (DEĞİŞTİRME):**
        [SEO BAŞLIK]
        (SEO başlığı)
        [---]
        [META AÇIKLAMA]
        (Meta açıklama)
        [---]
        [YAZI BAŞLIĞI]
        (Sanatsal H3 başlığı)
        [---]
        [İÇERİK]
        [H2]İlk Alt Başlık
        [P]Paragraf 1.
        [P]Paragraf 2.
        [H2]İkinci Alt Başlık
        [P]Paragraf 3.
        [---]
        [KAYNAKLAR]
        [Site Adı 1](URL 1)
        [Site Adı 2](URL 2)
        """
        
        gemini_response = generate_content_with_gemini(prompt)
        
        try:
            parts = gemini_response.strip().split('[---]')
            
            raw_seo_baslik = parts[0].replace('[SEO BAŞLIK]', '').replace('[SEO BAŞLIĞI]', '').strip()
            
            # Başlığı garantilemek için Gemini'ye tekrar sor
            title_fix_prompt = f"Aşağıdaki metinden sadece ana haber başlığını çıkar, başka hiçbir şey yazma. Eğer içinde 'görev', 'kozmik', 'pusula', 'anlatım', 'hazırım' gibi yorum kelimeleri varsa bunları kesinlikle at. Sadece net başlığı ver.\n\nMETİN: \"{raw_seo_baslik}\""
            seo_baslik = generate_content_with_gemini(title_fix_prompt).strip()

            meta_aciklama = parts[1].replace('[META AÇIKLAMA]', '').strip()
            yazi_basligi = parts[2].replace('[YAZI BAŞLIĞI]', '').strip()
            
            content_block = parts[3].replace('[İÇERİK]', '').strip()
            content_lines = content_block.split('\n')
            main_content_parts = []
            for line in content_lines:
                if line.startswith('[H2]'):
                    main_content_parts.append(('h2', line.replace('[H2]', '').strip()))
                elif line.startswith('[P]'):
                    main_content_parts.append(('p', line.replace('[P]', '').strip()))

            kaynaklar = [line.strip() for line in parts[4].replace('[KAYNAKLAR]', '').strip().split('\n') if line.strip()]

        except (IndexError, ValueError):
            return jsonify({"error": "Gemini'den gelen yanıt beklenilen formatta değil. Ayraçlar eksik olabilir."}), 500

        # 4. Adım: AI ile 2 farklı görsel üret
        ai_image1_prompt = f"Bilimsel illüstrasyon, fotogerçekçi: {yazi_basligi}. Asla canlı hayvan çizme. Sadece uzay, gezegenler, astronomi ve bilim teması. Görselde hiç yazı olmasın, sadece görsel öğeler olsun."
        ai_image2_prompt = f"Sanatsal uzay illüstrasyonu: {yazi_basligi}. Farklı bir perspektif ve stil. Uzay, gezegenler, astronomi teması. Görselde hiç yazı olmasın, sadece görsel öğeler olsun."
        
        ai_media1_url = None
        ai_media1_id = None
        ai_media2_url = None
        ai_media2_id = None
        
        # İlk görsel
        ai_image1_data = generate_ai_image(ai_image1_prompt)
        if ai_image1_data:
            logging.info("İlk AI görseli WordPress'e yükleniyor...")
            ai_media1_info = upload_image_to_wordpress(
                title=f"{seo_baslik} - AI Görsel 1", 
                image_data=ai_image1_data
            )
            if ai_media1_info and ai_media1_info.get('url'):
                ai_media1_url = ai_media1_info.get('url')
                ai_media1_id = ai_media1_info.get('id')
        
        # İkinci görsel
        ai_image2_data = generate_ai_image(ai_image2_prompt)
        if ai_image2_data:
            logging.info("İkinci AI görseli WordPress'e yükleniyor...")
            ai_media2_info = upload_image_to_wordpress(
                title=f"{seo_baslik} - AI Görsel 2", 
                image_data=ai_image2_data
            )
            if ai_media2_info and ai_media2_info.get('url'):
                ai_media2_url = ai_media2_info.get('url')
                ai_media2_id = ai_media2_info.get('id')

        # 5. Adım: Tamamen formatlanmış içeriği oluştur
        final_content = build_wordpress_content(
            title=yazi_basligi,
            main_content_parts=main_content_parts,
            nasa_image_url=None,  # Bu endpoint'te NASA görseli yok
            nasa_image_id=None,
            ai_image_url=ai_media1_url,
            ai_image_id=ai_media1_id,
            sources=kaynaklar,
            ai_image2_url=ai_media2_url,
            ai_image2_id=ai_media2_id
        )

        # 6. Adım: WordPress'e gönder
        logging.info(f"'{seo_baslik}' başlıklı yazı WordPress'e gönderiliyor...")
        post_details = post_to_wordpress(
            title=seo_baslik,
            content=final_content,
            meta_description=meta_aciklama,
            schedule_time=schedule_time
        )

        return jsonify({
            "status": "success",
            "message": "İçerik başarıyla üretildi ve WordPress'e taslak olarak gönderildi!",
            "post_url": post_details.get('_links', {}).get('self', [{}])[0].get('href')
        })

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"WordPress'e gönderirken hata oluştu: {e}"}), 500
    except Exception as e:
        return jsonify({"error": f"İşlem sırasında beklenmedik bir hata oluştu: {e}"}), 500


@app.route('/generate-and-post', methods=['POST'])
def generate_and_post_endpoint():
    """
    Panelden gelen manuel bir içerik üretim isteğini alır ve
    bu görevi arka planda çalışması için zamanlayıcıya ekler.
    """
    data = request.json
    topic = data.get('topic')
    schedule_time_str = data.get('schedule_time')

    if not topic:
        return jsonify({"error": "Lütfen bir içerik konusu belirtin."}), 400
    
    # Görevi hemen şimdi çalışması için zamanlayıcıya ekle
    scheduler.add_job(
        generate_and_post_logic_with_context, 
        'date', 
        run_date=datetime.now() + timedelta(seconds=1), 
        args=[topic, schedule_time_str]
    )

    return jsonify({
        "status": "success", 
        "message": f"'{topic}' konusu için içerik üretme görevi arka plana alındı. Birkaç dakika içinde 'Zamanlanmış Gönderiler' listesinde görebilirsiniz."
    })

def generate_and_post_logic_with_context(topic: str, schedule_time_str: str = None):
    """
    Flask uygulama bağlamı (app context) içinde generate_and_post_logic'i çalıştırır.
    Zamanlayıcı tarafından çağrılmak için gereklidir.
    """
    logging.info(f"[LOG] generate_and_post_logic_with_context BAŞLADI - Konu: {topic}")
    with app.app_context():
        final_schedule_time = None
        if schedule_time_str:
            final_schedule_time = datetime.fromisoformat(schedule_time_str).isoformat()
        else:
            now = datetime.now()
            smart_times = get_smart_schedule_times()
            for t in smart_times:
                if datetime.fromisoformat(t) > now:
                    final_schedule_time = t
                    break
            if not final_schedule_time:
                final_schedule_time = (now + timedelta(days=1)).replace(hour=8, minute=45).isoformat()
        
        try:
            # Mantık fonksiyonunu belirlenen zamanlama ile çağır
            generate_and_post_logic(topic, schedule_time=final_schedule_time)
            logging.info(f"BAŞARILI: '{topic}' konusu işlendi ve {final_schedule_time} tarihine zamanlandı.")
        except Exception as e:
            logging.error(f"*** HATA: '{topic}' konusu işlenirken arka planda bir hata oluştu: {e} ***")
    logging.info(f"[LOG] generate_and_post_logic_with_context BİTTİ - Konu: {topic}")


def post_nasa_apod_logic(schedule_time: str = None):
    """
    `post_nasa_apod` endpoint'inin ana mantığını içerir.
    """
    logging.info("[LOG] post_nasa_apod_logic BAŞLADI")
    try:
        # 1. Adım: NASA'dan veriyi al
        logging.info("NASA APOD verisi çekiliyor...")
        apod_data = get_nasa_apod()
        if not apod_data or 'url' not in apod_data:
            logging.error("NASA'dan APOD verisi alınamadı.")
            return jsonify({"error": "NASA'dan APOD verisi alınamadı."}), 502

        if apod_data.get("media_type") != "image":
            logging.warning(f"Bugünün APOD içeriği bir görsel değil, bir {apod_data.get('media_type')}. İşlem atlandı.")
            return jsonify({
                "status": "skipped",
                "message": f"Bugünün APOD içeriği bir görsel değil, bir {apod_data.get('media_type')}. İşlem atlandı."
            }), 200
            
        # 2. Adım: İçeriği Gemini ile zenginleştir
        logging.info("NASA içeriği Gemini'ye gönderiliyor...")
        ingilizce_baslik = apod_data['title']
        ingilizce_aciklama = apod_data['explanation']

        prompt = f"""
        Sen, galaktikuzay.com için yazan, Neil deGrasse Tyson gibi karmaşık konuları basit ve heyecan verici bir dille anlatan bir bilim iletişimcisisin. Görevin, sana verilen NASA verilerini analiz edip, SEO uyumlu, yapılandırılmış bir blog yazısı verisi oluşturmak.

        **KESİN KURALLAR:**
        1.  **SEO Başlığı:** 65 karakteri geçmeyen, merak uyandıran ve SADECE fotoğrafın konusunu açıklayan bir Türkçe başlık üret. 
            KESİNLİKLE YORUM EKLEME. "Harika bir görev", "Kozmik", "İşte analizim" gibi ifadeler KULLANMA. Sadece başlık olsun.
            Örnekler: "Mars Yüzeyindeki Gizemli Delik", "Perseverance'dan Yeni Görüntü", "Andromeda Galaksisi'nin Net Fotoğrafı"
        2.  **Meta Açıklama:** 160 karakteri geçmeyen, anahtar kelimeleri içeren bir meta açıklama yaz.
        3.  **Yazı Başlığı (H3):** İçerikte gösterilecek, daha sanatsal ve uzun bir başlık üret.
        4.  **İçerik Akışı:**
            *   İngilizce açıklamayı temel alarak, en az 400 kelimelik özgün bir metin oluştur.
            *   Metni `[H2]` etiketleriyle mantıksal alt başlıklara ayır.
            *   Her alt başlığın altına `[P]` etiketleriyle paragraflar ekle.
            *   Metin içinde `[Perseverance Gezgini](https://mars.nasa.gov/mars2020/)` gibi en az iki adet Markdown formatında dış link ver.
            *   Metne kişisel bir dokunuş kat.
        5.  **Kaynaklar:** Link verdiğin kaynakları ve ek olarak ana NASA APOD sayfasını (`[NASA APOD](https://apod.nasa.gov/apod/)`) "Kaynaklar" bölümü için listele.
        
        **ÇIKTI FORMATI (DEĞİŞTİRME):**
        [SEO BAŞLIK]
        (SEO başlığı)
        [---]
        [META AÇIKLAMA]
        (Meta açıklama)
        [---]
        [YAZI BAŞLIĞI]
        (Sanatsal H3 başlığı)
        [---]
        [İÇERİK]
        [H2]İlk Alt Başlık
        [P]Paragraf 1.
        [P]Paragraf 2.
        [H2]İkinci Alt Başlık
        [P]Paragraf 3.
        [---]
        [KAYNAKLAR]
        [Site Adı 1](URL 1)
        [Site Adı 2](URL 2)
        """
        
        gemini_response = generate_content_with_gemini(prompt)
        
        try:
            parts = gemini_response.strip().split('[---]')
            
            raw_seo_baslik = parts[0].replace('[SEO BAŞLIK]', '').replace('[SEO BAŞLIĞI]', '').strip()
            
            # Başlığı garantilemek için Gemini'ye tekrar sor
            title_fix_prompt = f"Aşağıdaki metinden sadece ana haber başlığını çıkar, başka hiçbir şey yazma. Eğer içinde 'görev', 'kozmik', 'pusula', 'anlatım', 'hazırım' gibi yorum kelimeleri varsa bunları kesinlikle at. Sadece net başlığı ver.\n\nMETİN: \"{raw_seo_baslik}\""
            seo_baslik = generate_content_with_gemini(title_fix_prompt).strip()

            meta_aciklama = parts[1].replace('[META AÇIKLAMA]', '').strip()
            yazi_basligi = parts[2].replace('[YAZI BAŞLIĞI]', '').strip()
            
            content_block = parts[3].replace('[İÇERİK]', '').strip()
            content_lines = content_block.split('\n')
            main_content_parts = []
            for line in content_lines:
                if line.startswith('[H2]'):
                    main_content_parts.append(('h2', line.replace('[H2]', '').strip()))
                elif line.startswith('[P]'):
                    main_content_parts.append(('p', line.replace('[P]', '').strip()))

            kaynaklar = [line.strip() for line in parts[4].replace('[KAYNAKLAR]', '').strip().split('\n') if line.strip()]

        except (IndexError, ValueError):
            logging.error("Gemini'den gelen yanıt beklenilen formatta değil. Ayraçlar eksik olabilir.")
            return jsonify({"error": "Gemini'den gelen yanıt beklenilen formatta değil. Ayraçlar eksik olabilir."}), 500

        # 3. Adım: Görseli WordPress'e yükle
        logging.info(f"'{seo_baslik}' başlıklı görsel WordPress'e yükleniyor...")
        media_info = upload_image_to_wordpress(title=seo_baslik, image_url=apod_data['hdurl'])
        if not media_info:
            logging.error("NASA görseli WordPress'e yüklenemedi.")
            return jsonify({"error": "NASA görseli WordPress'e yüklenemedi."}), 500
        
        media_id = media_info['id']
        media_url = media_info['url']

        # 4. Adım: AI ile ek görsel üret
        ai_image_prompt = f"Bilimsel illüstrasyon, fotogerçekçi: {yazi_basligi}. Asla canlı hayvan veya leopar deseni çizme. Sadece Mars gezegeni, uzay ve jeolojik kaya oluşumları teması."
        ai_image_data = generate_ai_image(ai_image_prompt)
        ai_media_url = None
        ai_media_id = None
        if ai_image_data:
            logging.info("Üretilen AI görseli WordPress'e yükleniyor...")
            ai_media_info = upload_image_to_wordpress(
                title=f"{seo_baslik} - Yapay Zeka Yorumu", 
                image_data=ai_image_data
            )
            if ai_media_info and ai_media_info.get('url'):
                ai_media_url = ai_media_info.get('url')
                ai_media_id = ai_media_info.get('id')

        # 5. Adım: Tamamen formatlanmış içeriği oluştur
        final_content = build_wordpress_content(
            title=yazi_basligi,
            main_content_parts=main_content_parts,
            nasa_image_url=media_url,
            nasa_image_id=media_id,
            ai_image_url=ai_media_url,
            ai_image_id=ai_media_id,
            sources=kaynaklar
        )
        
        # Telif hakkı bilgisini ekleyelim, eğer varsa
        copyright_info = f"Görsel Sahibi: {apod_data['copyright']}" if 'copyright' in apod_data else ""
        final_content += f"\n\n<!-- wp:paragraph -->\n<p><em>{copyright_info}</em></p>\n<!-- /wp:paragraph -->"

        # 6. Adım: Yazıyı WordPress'e gönder
        today_date = datetime.now().strftime("%d.%m.%Y")
        final_title = f"Günün Astronomi Fotoğrafı ({today_date}): {seo_baslik}"  # Günün astronomi fotoğrafı ve tarih ekle
        logging.info(f"'{final_title}' başlıklı yazı WordPress'e gönderiliyor...")
        post_details = post_to_wordpress(final_title, final_content, featured_media_id=media_id, meta_description=meta_aciklama, schedule_time=schedule_time)
        logging.info(f"BAŞARILI: NASA APOD içeriği gönderildi. Post ID: {post_details.get('id')}")
    
    except ValueError as e:
        logging.error(f"post_nasa_apod_logic sırasında bir hata oluştu: {e}")
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logging.error(f"İşlem sırasında beklenmedik bir hata oluştu: {e}")
        return jsonify({"error": f"İşlem sırasında beklenmedik bir hata oluştu: {e}"}), 500
    logging.info("[LOG] post_nasa_apod_logic BİTTİ")


def get_wordpress_posts(limit=100):
    """
    WordPress sitesinden sadece 'yayınlanmış' yazıların başlıklarını çeker.
    """
    wp_url = os.getenv("WORDPRESS_URL")
    wp_user = os.getenv("WORDPRESS_USER")
    wp_password = os.getenv("WORDPRESS_APP_PASSWORD")
    
    if not all([wp_url, wp_user, wp_password]):
        logging.warning("WordPress kimlik bilgileri eksik")
        return []
    
    credentials = f"{wp_user}:{wp_password}"
    base64_encoded_auth = base64.b64encode(credentials.encode()).decode("utf-8")

    api_url = f"{wp_url.rstrip('/')}/wp-json/wp/v2/posts"
    
    headers = {
        'Authorization': f'Basic {base64_encoded_auth}'
    }
    
    params = {
        'per_page': limit,
        'status': 'publish',  # Sadece yayınlanmış yazıları al
        '_fields': 'title'      # Sadece başlık alanını al
    }
    
    try:
        response = requests.get(api_url, headers=headers, params=params, timeout=30)
        response.raise_for_status()
        
        posts = response.json()
        titles = [post['title']['rendered'] for post in posts]
        logging.info(f"WordPress'ten {len(titles)} yazı başlığı alındı")
        return titles
        
    except Exception as e:
        logging.error(f"WordPress yazıları alınırken hata: {e}")
        return []


def discover_trending_topics():
    """
    Google'da güncel uzay ve astronomi konularını arar ve ilgi çekici konular bulur.
    """
    try:
        # Güncel uzay konuları için arama terimleri - İngilizce + çeşitli ve viral
        search_terms = [
            "space news 2025",
            "astronomy discoveries",
            "meteor shower 2025",
            "space missions",
            "planet discoveries",
            "space technology",
            "astronaut news",
            "black hole discoveries",
            "star formation",
            "galaxy discoveries",
            "space station",
            "rocket technology",
            "space tourism",
            "Mars missions",
            "Jupiter discoveries",
            "Saturn rings",
            "space telescopes",
            "exoplanet discoveries",
            "NASA latest news",
            "ESA space missions",
            "space exploration",
            "cosmic phenomena",
            "solar system news",
            "space science breakthroughs",
            # Tek kelimelik aramalar
            "space",
            "NASA",
            "astronomy",
            "cosmos",
            "universe",
            "galaxy",
            "planet",
            "asteroid",
            "comet",
            "moon",
            "sun",
            "star"
        ]
        
        all_results = []
        
        for term in search_terms:
            logging.info(f"'{term}' aranıyor...")
            results = search_google(term, time_filter="qdr:d")  # Son 24 saat filtresi
            if results:
                all_results.extend(results[:3])  # Her terimden en fazla 3 sonuç
        
        # Sonuçları Gemini'ye gönder ve ilgi çekici konuları filtrele
        if all_results:
            topics_prompt = f"""
            Sen bir uzay ve astronomi içerik editörüsün. Aşağıdaki güncel haberleri analiz et ve galaktikuzay.com için en ilgi çekici 3 konuyu seç.
            
            ÖNEMLİ: Bu konular NASA APOD'dan TAMAMEN FARKLI olmalı. NASA APOD zaten günlük astronomi fotoğrafı için kullanılıyor.
            
            Seçim kriterleri:
            1. Türkçe okuyucular için anlaşılır olmalı
            2. Görsel içerik üretilebilir olmalı
            3. SEO dostu olmalı
            4. Viral potansiyeli olmalı
            5. BİRBİRİNDEN TAMAMEN FARKLI KONULAR OLMALI - Aynı gezegen, aynı konu olmasın
            6. NASA APOD'dan FARKLI olmalı - Mars keşifleri, Perseverance, Curiosity gibi NASA APOD konuları seçme
            7. Çeşitlilik: Jüpiter, Satürn, kara delik, yıldız, galaksi, uzay teknolojisi, meteor yağmurları, exoplanet gibi farklı alanlar
            8. Güncel haberler - son 24 saat içindeki gelişmeler
            
            Haberler:
            {chr(10).join([f"- {item.get('title', '')}: {item.get('link', '')}" for item in all_results[:10]])}
            
            Sadece konu başlıklarını, her satırda bir tane olacak şekilde listele. Açıklama ekleme.
            """
            
            topics_response = generate_content_with_gemini(topics_prompt)
            topics = [line.strip() for line in topics_response.split('\n') if line.strip()]
            
            # Her konuyu temizle
            clean_topics = []
            for topic in topics:
                # clean_topic = clean_gemini_output(topic) # Removed as per edit hint
                if len(topic) > 10 and len(topic) < 100:
                    clean_topics.append(topic)
            
            logging.info(f"Keşfedilen konular: {clean_topics}")
            return clean_topics[:3]  # En fazla 3 konu döndür
        
        return []
        
    except Exception as e:
        logging.error(f"Konu keşfi sırasında hata: {e}")
        return []


def is_semantically_similar(new_topic: str, existing_titles: list) -> bool:
    """
    Gemini kullanarak yeni bir konunun mevcut başlıklarla anlamsal olarak benzer olup olmadığını kontrol eder.
    """
    if not existing_titles:
        return False

    try:
        titles_text = "\n".join([f"- {title}" for title in existing_titles])
        prompt = f"""
        Aşağıdaki 'YENİ KONU'nun, 'MEVCUT BAŞLIKLAR' listesindeki herhangi bir başlıkla anlamsal olarak aynı temel olayı, keşfi veya haberi anlatıp anlatmadığını analiz et.
        
        Örneğin, "Perseid meteor yağmuru başlıyor" ile "Gökyüzünde meteor şöleni" anlamsal olarak benzerdir. Ama "2025 Perseid meteor yağmuru" ile "2024 Perseid meteor yağmuru" farklıdır.
        
        YENİ KONU:
        "{new_topic}"
        
        MEVCUT BAŞLIKLAR:
        {titles_text}
        
        Bu yeni konu, listedeki herhangi bir başlıkla anlamsal olarak benzer mi? Sadece 'EVET' veya 'HAYIR' olarak cevap ver.
        """
        
        response = generate_content_with_gemini(prompt)
        
        # Yanıtı temizle ve kontrol et
        cleaned_response = response.strip().upper()
        logging.info(f"Anlamsal benzerlik kontrolü: Yeni konu='{new_topic}', Cevap='{cleaned_response}'")
        
        return "EVET" in cleaned_response

    except Exception as e:
        logging.error(f"Anlamsal benzerlik kontrolü sırasında hata: {e}")
        # Hata durumunda, riske atmamak için benzer kabul et
        return True


@app.route('/generate-daily-content', methods=['POST'])
def generate_daily_content_endpoint():
    """
    Günlük içerik üretimini manuel olarak tetiklemek için endpoint.
    """
    # Süreci arka planda başlat
    scheduler.add_job(trigger_daily_content_generation_with_context)
    return jsonify({
        "status": "success",
        "message": "Günlük içerik üretim süreci arka planda başlatıldı. Terminal loglarını kontrol edin."
    })


def trigger_daily_content_generation_with_context():
    """
    Flask uygulama bağlamı (app context) içinde trigger_daily_content_generation'ı çalıştırır.
    Zamanlayıcı tarafından çağrılmak için gereklidir.
    """
    logging.info("[LOG] trigger_daily_content_generation_with_context BAŞLADI")
    with app.app_context():
        trigger_daily_content_generation()
    logging.info("[LOG] trigger_daily_content_generation_with_context BİTTİ")

def trigger_daily_content_generation():
    logging.info("="*50)
    logging.info(f"OTOMATİK GÜNLÜK İÇERİK ÜRETİMİ BAŞLATILDI - {datetime.now()}")
    logging.info("="*50)

    # Flask uygulama bağlamı (application context) içinde çalıştır
    with app.app_context():
        # 0. Adım: Akıllı zamanlamaları oluştur
        schedule_times = get_smart_schedule_times()
        logging.info(f"\nBugünün yayın planı oluşturuldu: {schedule_times}\n")

        # 1. NASA APOD içeriği üret ve zamanla
        logging.info("\n=== 1/4: NASA APOD İçeriği Üretiliyor ve Zamanlanıyor ===\n")
        try:
            # Yerel URL'ye istek göndermek yerine doğrudan fonksiyonu çağır
            post_nasa_apod_logic_with_context(schedule_time=schedule_times[0])
            logging.info(f"\n--- NASA APOD İçeriği Başarıyla Zamanlandı: {schedule_times[0]} ---\n")
        except Exception as e:
            logging.error(f"\n*** HATA: NASA APOD İçeriği Üretilemedi: {e} ***\n")

        # 2. Mevcut yazıları kontrol et
        logging.info("\n=== 2/4: Mevcut Yazılar Kontrol Ediliyor ===\n")
        existing_titles = get_wordpress_posts()
        
        # 3. Güncel konuları keşfet
        logging.info("\n=== 3/4: Güncel Konular Keşfediliyor ===\n")
        trending_topics = discover_trending_topics()
        
        # 4. Benzersiz konulardan 3 içerik üret ve zamanla
        logging.info(f"\n=== 4/4: 3 Adet Benzersiz Konu İçin İçerik Üretimi Başlatılıyor ===\n")
        
        published_google_posts = 0
        topic_index = 0
        
        while published_google_posts < 3 and topic_index < len(trending_topics):
            topic = trending_topics[topic_index]
            topic_index += 1 # Bir sonraki deneme için indeksi artır

            logging.info(f"\n--- Aday Konu: '{topic}' ---")
            
            if is_semantically_similar(topic, existing_titles):
                logging.info(f"Anlamsal olarak benzer konu atlandı: {topic}")
                continue # Bu konuyu atla ve döngünün başına dön

            try:
                post_schedule_time = schedule_times[published_google_posts + 1]
                # Doğrudan fonksiyonu çağır ve zamanlama bilgisini gönder
                generate_and_post_logic_with_context(topic, schedule_time_str=post_schedule_time)
                
                logging.info(f"\n--- Konu '{topic}' Başarıyla Zamanlandı: {post_schedule_time} ---\n")
                published_google_posts += 1 # Başarılı yayın sayısını artır
                existing_titles.append(topic) # Gelecek kontroller için listeye ekle

            except Exception as e:
                logging.error(f"\n*** HATA: Konu '{topic}' İşlenemedi: {e} ***\n")
        
        if published_google_posts < 3:
            logging.warning(f"\n!!! UYARI: Hedeflenen 3 Google içeriği yerine sadece {published_google_posts} adet üretilebildi. Konu havuzu yetersiz olabilir.")

    logging.info("="*50)
    logging.info(f"OTOMATİK GÜNLÜK İÇERİK ÜRETİMİ TAMAMLANDI - {datetime.now()}")
    logging.info("="*50)


# Zamanlayıcıyı KUR ve BAŞLAT (Gunicorn'un erişebileceği yer)
# ----------------------------------------------------------------
scheduler.add_job(trigger_daily_content_generation_with_context, 'cron', hour=8, minute=45)
scheduler.start()

# Uygulama kapatıldığında zamanlayıcıyı güvenli bir şekilde kapat
atexit.register(lambda: scheduler.shutdown())
# ----------------------------------------------------------------

if __name__ == '__main__':
    # Bu blok artık sadece bilgisayarınızda yerel test için kullanılacak.
    # Render bu kısmı çalıştırmaz.
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
