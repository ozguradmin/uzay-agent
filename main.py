from flask import Flask, jsonify, request, render_template
from dotenv import load_dotenv
import os
import requests
import base64
from flask import request
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import pytz
import re
from PIL import Image
from io import BytesIO
from google.oauth2 import service_account
from google.auth.transport.requests import Request
import google.auth
import google.auth.transport.requests
import threading
import time
import logging
import random
import dateutil.parser
import json
from urllib.parse import urlparse, parse_qs

# .env dosyasındaki ortam değişkenlerini yükle
load_dotenv()

# logging'i ayarla
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = Flask(__name__)

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

def resolve_redirect_url(url: str):
    """
    Verilen bir URL'yi (özellikle Google'ın yönlendirme linklerini) takip ederek
    nihai (gerçek) URL'yi bulur. Daha güçlü hata kontrolü ve fallback mekanizması.
    """
    if not url or len(url.strip()) == 0:
        logging.warning("Boş URL verildi.")
        return url
    
    # Google'ın yönlendirme linklerini kontrol et
    if "vertexaisearch.cloud.google.com" in url or "google.com/search" in url or "google.com/url" in url:
        try:
            # İlk önce HEAD isteği dene (daha hızlı)
            response = requests.head(url, timeout=15, allow_redirects=True, 
                                   headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
            
            if response.status_code == 200:
                final_url = response.url
                # Eğer final URL hala geçersiz görünüyorsa, GET isteği dene
                if "google.com" in final_url and "search" in final_url:
                    response = requests.get(url, timeout=15, allow_redirects=True,
                                          headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
                    final_url = response.url
                
                logging.info(f"Yönlendirme başarıyla çözüldü: '{url[:50]}...' -> '{final_url[:50]}...'")
                return final_url
            else:
                logging.warning(f"HEAD isteği başarısız oldu (Status: {response.status_code}): {url}")
                return url
                
        except requests.RequestException as e:
            logging.error(f"URL yönlendirmesi çözülürken hata: {url[:50]}..., Hata: {e}")
            # Hata durumunda orijinal URL'yi döndür ama logla
            return url
        except Exception as e:
            logging.error(f"Beklenmedik hata URL yönlendirmesi sırasında: {e}")
            return url
    
    # Normal URL'ler için doğrulama yap
    if url.startswith(('http://', 'https://')):
        return url
    
    # Geçersiz URL formatı
    logging.warning(f"Geçersiz URL formatı: {url}")
    return url

def search_google(query: str, num_results: int = 10):
    """
    Gemini'nin yerleşik 'google_search' aracını kullanarak bir arama sorgusu gerçekleştirir
    ve son 24-48 saat içindeki sonuçları döndürür.
    """
    logging.info(f"Gemini arama aracıyla sorgu yapılıyor: '{query}'")
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        raise ValueError("Gemini API anahtarı .env dosyasında eksik.")

    # Araç kullanımını destekleyen bir model kullanılıyor
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent?key={gemini_api_key}"
    
    headers = {'Content-Type': 'application/json'}

    # Gemini'ye sonuçları nasıl formatlaması gerektiğini söyleyen sistem talimatı
    system_prompt = """
    Sen bir arama sonucu formatlama aracısın. EN BİRİNCİL GÖREVİN, kullanıcının sorgusu için bir Google araması yapmak ve sonuçları KESİNLİKLE son 24 saat (past day) ile sınırlamaktır. Diğer tüm kriterlerden önce bu zaman filtresi gelmelidir. En alakalı 5-7 sonucu döndür.
    Yanıtını TEK BİR JSON nesnesi olarak formatlamalısın. Bu nesne "items" adında tek bir anahtar içermelidir.
    "items" anahtarının değeri, her biri "title", "link" ve "snippet" anahtarlarına sahip nesnelerden oluşan bir dizi olmalıdır.
    Alakalı sonuç bulamazsan, {"items": []} gibi boş bir "items" dizisi içeren bir nesne döndür.
    JSON nesnesinden önce veya sonra herhangi bir metin veya markdown formatlaması ekleme.
    """

    payload = {
        "contents": [{"parts": [{"text": query}]}],
        "tools": [{"google_search": {}}],
        "system_instruction": {"parts": [{"text": system_prompt}]}
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=90)
        response.raise_for_status()
        
        response_json = response.json()
        candidates = response_json.get('candidates', [])
        
        if not (candidates and candidates[0].get('content', {}).get('parts', [])):
            logging.error(f"Gemini arama aracından geçerli bir yanıt alınamadı. Ham yanıt: {response_json}")
            raise ValueError("Gemini API'sinden arama aracıyla geçerli bir yanıt alınamadı.")

        raw_text = candidates[0]['content']['parts'][0]['text']
        
        # Yanıtı temizleyerek sadece JSON'u al
        json_start = raw_text.find('{')
        json_end = raw_text.rfind('}')
        if json_start == -1 or json_end == -1:
            logging.error(f"API yanıtında beklenen JSON formatı bulunamadı. Gelen yanıt: {raw_text}")
            return [] # Boş liste döndür
            
        json_string = raw_text[json_start:json_end + 1]
        data = json.loads(json_string)
        
        found_items = data.get("items", [])
        
        # URL'leri temizle ve standartlaştır
        cleaned_items = []
        for item in found_items:
            original_link = item.get('link')
            if original_link:
                item['link'] = resolve_redirect_url(original_link)
            cleaned_items.append(item)

        if cleaned_items:
            for i, item in enumerate(cleaned_items):
                 logging.info(f"  Gemini Arama Sonucu {i+1}: Başlık='{item.get('title')}', Link='{item.get('link')}'")
        else:
            logging.info("  Gemini aramasında sonuç bulunamadı.")
            
        return cleaned_items

    except requests.exceptions.RequestException as e:
        logging.error(f"Gemini arama aracıyla arama sırasında bir hata oluştu: {e}")
        if e.response:
            logging.error(f"Hata detayı: {e.response.text}")
        return []
    except json.JSONDecodeError as e:
        logging.error(f"Gemini'den dönen JSON yanıtı ayrıştırılamadı: {e}")
        logging.error(f"Ayrıştırılamayan metin: {raw_text}")
        return []
    except Exception as e:
        logging.error(f"Gemini arama sırasında beklenmedik bir hata: {e}")
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
        try:
            response.raise_for_status()
        except requests.exceptions.RequestException as http_err:
            logging.error(f"!!! HATA: Gemini Görsel API HTTP hatası: {http_err}")
            logging.error(f"Yanıt Durum Kodu: {response.status_code}")
            logging.error(f"Yanıt Metni: {response.text}")
            return None

        response_data = response.json()
        # logging.info(f"Gemini Görsel API ham yanıtı: {response_data}") # Ham yanıtı sadece hata durumunda loglayacağız
        
        # Gelen base64 verisini decode et
        if 'candidates' in response_data and len(response_data['candidates']) > 0:
            candidate = response_data['candidates'][0]
            if 'content' in candidate and 'parts' in candidate['content']:
                for part in candidate['content']['parts']:
                    if 'inlineData' in part and 'data' in part['inlineData']:
                        image_bytes = base64.b64decode(part['inlineData']['data'])
                        logging.info("AI görseli başarıyla üretildi.")
                        return image_bytes
        
        logging.error(f"!!! HATA: AI görseli üretilemedi - yanıt formatı beklenmeyen veya görsel verisi yok. Ham yanıt: {response_data}")
        return None

    except Exception as e:
        logging.error(f"!!! HATA: AI görseli üretimi sırasında genel hata: {e}")
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


def generate_and_post_logic(topic: str, source_articles: list, schedule_time: str = None):
    """
    Verilen bir konu ve kaynak makaleler listesiyle içerik üretir ve yayınlar.
    Artık kendi aramasını yapmıyor, hazır kaynakları kullanıyor.
    """
    if not topic or not source_articles or len(source_articles) == 0:
        logging.error(f"HATA: '{topic}' için kaynak makaleler bulunamadı veya boş.")
        return False

    # Kaynak makalelerin en az 2 tane olması gerekiyor
    if len(source_articles) < 2:
        logging.error(f"HATA: '{topic}' için yeterli kaynak makale bulunamadı (sadece {len(source_articles)} adet).")
        return False

    try:
        # 1. Adım: Kaynakları metne dönüştür (Arama adımı kaldırıldı)
        sources_text = "\n".join([f"- {article['title']}: {article['link']}" for article in source_articles])

        # 2. Adım: İçerik Üretme
        logging.info("Hazır arama sonuçları Gemini'ye gönderiliyor ve içerik üretiliyor...")

        prompt = f"""
        Sen, galaktikuzay.com için yazan, Neil deGrasse Tyson gibi karmaşık konuları basit ve heyecan verici bir dille anlatan bir bilim iletişimcisisin. Görevin, verilen konuyu ve kaynakları analiz edip, SEO uyumlu, yapılandırılmış bir blog yazısı verisi oluşturmak.

        **ANA KONU:** {topic}
        
        **KULLANILACAK KAYNAKLAR (Bu kaynakların dışına çıkma):**
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
            logging.error(f"Gemini'den gelen yanıt '{topic}' için beklenilen formatta değil. Ayraçlar eksik olabilir.")
            return False

        # 3. Adım: AI ile 2 farklı görsel üret
        ai_image1_prompt = f"Bilimsel illüstrasyon, fotogerçekçi: {yazi_basligi}. Asla canlı hayvan çizme. Sadece uzay, gezegenler, astronomi ve bilim teması. Görselde hiç yazı olmasın, sadece görsel öğeler olsun."
        ai_image2_prompt = f"Sanatsal uzay illüstrasyonu: {yazi_basligi}. Farklı bir perspektif ve stil. Uzay, gezegenler, astronomi teması. Görselde hiç yazı olmasın, sadece görsel öğeler olsun."
        
        ai_media1_url = None
        ai_media1_id = None
        ai_media2_url = None
        ai_media2_id = None
        featured_media_id_to_use = None # Öne çıkarılacak görseli belirlemek için
        
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
                featured_media_id_to_use = ai_media1_id # İlk görseli öne çıkan yap
        
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
                # Eğer ilk görsel başarısız olduysa, ikinciyi öne çıkan yap
                if not featured_media_id_to_use:
                    featured_media_id_to_use = ai_media2_id
        
        if not featured_media_id_to_use:
            logging.warning(f"'{seo_baslik}' konusu için her iki AI görseli de üretilemedi. Yazı görsel olmadan yayınlanacak.")

        # 4. Adım: Tamamen formatlanmış içeriği oluştur
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

        # 5. Adım: WordPress'e gönder
        logging.info(f"'{seo_baslik}' başlıklı yazı WordPress'e gönderiliyor...")
        post_to_wordpress(
            title=seo_baslik,
            content=final_content,
            featured_media_id=featured_media_id_to_use,
            meta_description=meta_aciklama,
            schedule_time=schedule_time
        )

        return True # Başarılı olduğunu belirtmek için True döndür

    except Exception as e:
        logging.error(f"'{topic}' işlenirken beklenmedik bir hata oluştu: {e}")
        return False


def generate_and_post_logic_with_context(topic: str, source_articles: list, schedule_time_str: str = None):
    """
    Flask uygulama bağlamı (app context) içinde generate_and_post_logic'i çalıştırır.
    Zamanlayıcı tarafından çağrılmak için gereklidir. Başarı durumunu (True/False) döndürür.
    """
    logging.info(f"[LOG] generate_and_post_logic_with_context BAŞLADI - Konu: {topic}")
    success = False
    with app.app_context():
        final_schedule_time = None
        if schedule_time_str:
            final_schedule_time = datetime.fromisoformat(schedule_time_str).isoformat()
        else:
            # Bu durum normalde yaşanmamalı ama bir yedek mekanizma
            now = datetime.now()
            final_schedule_time = (now + timedelta(hours=1)).isoformat()
        
        try:
            # Mantık fonksiyonunu belirlenen zamanlama ve kaynaklarla çağır
            success = generate_and_post_logic(topic, source_articles=source_articles, schedule_time=final_schedule_time)
            if success:
                logging.info(f"BAŞARILI: '{topic}' konusu işlendi ve {final_schedule_time} tarihine zamanlandı.")
            else:
                logging.warning(f"UYARI: '{topic}' konusu işlenemedi veya atlandı (örneğin, Gemini format hatası).")
        except Exception as e:
            logging.error(f"*** HATA: '{topic}' konusu işlenirken arka planda bir hata oluştu: {e} ***")
            success = False
    logging.info(f"[LOG] generate_and_post_logic_with_context BİTTİ - Konu: {topic}")
    return success


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
            return None

        # APOD verisinin bugüne ait ve bir görsel olup olmadığını kontrol et
        today_date_str = datetime.now().strftime("%Y-%m-%d")
        apod_date = apod_data.get('date')
        media_type = apod_data.get("media_type")

        if apod_date != today_date_str:
            logging.warning(f"Bugünün APOD içeriği mevcut değil. Gelen tarih: {apod_date}, Beklenen tarih: {today_date_str}. İşlem atlandı.")
            return None
        
        if media_type != "image":
            logging.warning(f"Bugünün APOD içeriği bir görsel değil, bir '{media_type}'. İşlem atlandı.")
            return None
            
        # 2. Adım: İçeriği Gemini ile zenginleştir
        logging.info("NASA içeriği Gemini'ye gönderiliyor...")
        # NASA'dan gelen başlık ve açıklamayı prompt'a ekle
        ingilizce_baslik = apod_data.get('title', 'Başlık Yok')
        ingilizce_aciklama = apod_data.get('explanation', 'Açıklama Yok')
        
        prompt = f"""
        Sen, galaktikuzay.com için yazan, Neil deGrasse Tyson gibi karmaşık konuları basit ve heyecan verici bir dille anlatan bir bilim iletişimcisisin. Görevin, sana verilen NASA verilerini analiz edip, SEO uyumlu, yapılandırılmış bir blog yazısı verisi oluşturmak.

        **VERİLEN NASA BİLGİLERİ:**
        - Başlık: {ingilizce_baslik}
        - Açıklama: {ingilizce_aciklama}

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
        return True # Başarılı olduğunu belirtmek için True döndür
    
    except ValueError as e:
        logging.error(f"post_nasa_apod_logic sırasında bir hata oluştu: {e}")
        return None
    except Exception as e:
        logging.error(f"İşlem sırasında beklenmedik bir hata oluştu: {e}")
        return None
    finally:
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
            "latest space news today",
            "recent astronomy discoveries",
            "new exoplanet discoveries",
            "NASA mission updates latest",
            "ESA space news recent",
            "cutting-edge space technology breakthroughs",
            "black hole discoveries recent",
            "galaxy evolution latest research",
            "cosmic phenomena breaking news",
            "solar system recent observations",
            "space exploration recent findings",
            "astrophysics news today",
            "space science latest breakthroughs",
            "current events in space",
            "universe news recent",
            "meteor shower news today",
            "space weather alerts recent",
            "international space station news",
            "mars rover updates latest",
            "jupiter discoveries recent",
            "saturn discoveries new",
            "space telescope findings today",
            "cosmic ray discoveries recent",
            "nebula discoveries latest",
            "star formation news recent",
            "space launch news today",
            "planetary science breakthroughs",
            "space debris news recent",
            "solar flare news today",
            "comet discoveries recent"
        ]
        
        all_results = []
        unique_links = set()

        for term in search_terms:
            logging.info(f"'{term}' için arama yapılıyor...")
            results = search_google(term, num_results=5) # Her terimden daha az ama odaklı sonuç al
            if results:
                for item in results:
                    # URL bazında tekilleştirme
                    link = item.get('link')
                    if link and link not in unique_links:
                        all_results.append(item)
                        unique_links.add(link)
        
        # Sonuçları Gemini'ye gönder ve ilgi çekici konuları filtrele
        if all_results:
            # Konu keşfi için tüm benzersiz sonuçları gönderelim
            news_items_text = "\n".join([f"- {item.get('title', '')}: {item.get('link', '')}" for item in all_results])
            logging.info(f"Gemini'ye gönderilen ham arama sonuçları:\n{news_items_text}")

            criteria = """Seçim kriterleri:
            1. Türkçe okuyucular için anlaşılır olmalı
            2. Görsel içerik üretilebilir olmalı
            3. SEO dostu olmalı
            4. Viral potansiyeli olmalı
            5. BİRBİRİNDEN TAMAMEN FARKLI KONULAR OLMALI - Aynı gezegen, aynı konu olmasın
            6. NASA APOD'dan FARKLI olmalı - Mars keşifleri, Perseverance, Curiosity gibi NASA APOD konuları seçme
            7. Çeşitlilik: Jüpiter, Satürn, kara delik, yıldız, galaksi, uzay teknolojisi, meteor yağmurları, exoplanet gibi farklı alanlar
            8. EN ÖNEMLİ KURAL: Konular KESİNLİKLE son 24 saat içindeki gelişmelere dayanmalıdır. 'on this day' gibi tarihi olaylar veya eski haberler KESİNLİKLE YASAKTIR."""

            topics_prompt = f"""Sen bir uzay ve astronomi içerik editörüsün. Aşağıdaki güncel haberleri analiz et ve galaktikuzay.com için en ilgi çekici 3 konuyu seç.
            
ÖNEMLİ: Bu konular NASA APOD'dan TAMAMEN FARKLI olmalı. NASA APOD zaten günlük astronomi fotoğrafı için kullanılıyor.
            
{criteria}
            
Haberler:
{news_items_text}
            
Sadece konu başlıklarını, her satırda bir tane olacak şekilde listele. Açıklama ekleme."""
            
            logging.info(f"Gemini'ye gönderilen konu keşfi promptu: \n---\n{topics_prompt}\n---")
            topics_response = generate_content_with_gemini(topics_prompt)
            logging.info(f"Gemini'den dönen ham konu fikirleri: \n---\n{topics_response}\n---")
            topics = [line.strip() for line in topics_response.split('\n') if line.strip()]
            
            # Her konuyu temizle
            clean_topics = []
            for topic in topics:
                # clean_topic = clean_gemini_output(topic) # Removed as per edit hint
                if len(topic) > 10 and len(topic) < 100:
                    clean_topics.append(topic)
            
            logging.info(f"Keşfedilen konular: {clean_topics}")
            # Dönen obje sadece konuları değil, kaynakları da içermeli
            return {
                "topics": clean_topics[:3],
                "sources": all_results
            }
        
        return None # Hiçbir şey bulunamadıysa None döndür
        
    except Exception as e:
        logging.error(f"Konu keşfi sırasında hata: {e}")
        return None


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


def trigger_daily_content_generation():
    logging.info("="*50)
    logging.info(f"OTOMATİK GÜNLÜK İÇERİK ÜRETİMİ BAŞLATILDI - {datetime.now()}")
    logging.info("="*50)

    # Flask uygulama bağlamı (application context) içinde çalıştır
    with app.app_context():
        # Başlamadan önce mevcut yazıları bir kere çekelim
        existing_titles = get_wordpress_posts()
        
        # 0. Adım: Akıllı zamanlamaları oluştur
        schedule_times = get_smart_schedule_times()
        logging.info(f"\nBugünün yayın planı oluşturuldu: {schedule_times}\n")

        # 1. NASA APOD içeriği üret ve zamanla (eğer zaten yayınlanmamışsa)
        logging.info("\n=== 1/4: NASA APOD İçeriği Üretiliyor ve Zamanlanıyor ===\n")
        
        today_date_str_for_title = datetime.now().strftime("%d.%m.%Y")
        expected_apod_title_prefix = f"Günün Astronomi Fotoğrafı ({today_date_str_for_title})"
        
        apod_already_posted = any(title.startswith(expected_apod_title_prefix) for title in existing_titles)

        if apod_already_posted:
            logging.info(f"'{expected_apod_title_prefix}' başlıklı APOD yazısı bugün zaten yayınlanmış. Bu adım atlanıyor.")
        else:
            try:
                logging.info(f"NASA APOD için 'post_nasa_apod_logic' çağrılıyor. Zaman: {schedule_times[0]}")
                result = post_nasa_apod_logic(schedule_time=schedule_times[0])
                if result:
                    logging.info(f"NASA APOD içeriği başarıyla oluşturuldu ve {schedule_times[0]} tarihine zamanlandı.")
                else:
                    logging.warning(f"NASA APOD içeriği oluşturulamadı veya atlandı. Zaman: {schedule_times[0]}")
            except ValueError as e:
                logging.error(f"NASA APOD içeriği oluşturulurken veya zamanlanırken bir değer hatası oluştu: {e}")
            except Exception as e:
                logging.error(f"NASA APOD içeriği oluşturulurken veya zamanlanırken beklenmedik bir hata oluştu: {e}")

        # 2. Mevcut yazıları tekrar kontrol etmeye gerek yok, en başta aldık
        logging.info("\n=== 2/4: Mevcut Yazılar Kontrol Ediliyor (Adım atlandı, başlangıçta yapıldı) ===\n")
        
        # 3. Güncel konuları keşfet
        logging.info("\n=== 3/4: Güncel Konular Keşfediliyor ===\n")
        discovery_result = discover_trending_topics()
        
        trending_topics = []
        all_found_articles = []

        if discovery_result:
            trending_topics = discovery_result.get("topics", [])
            all_found_articles = discovery_result.get("sources", [])

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
                # Konuyu ve TÜM bulunan kaynakları doğrudan fonksiyona geçir
                logging.info(f"'{topic}' konusu için {len(all_found_articles)} adet kaynak makale kullanılacak.")
                success = generate_and_post_logic_with_context(
                    topic, 
                    source_articles=all_found_articles, 
                    schedule_time_str=post_schedule_time
                )
                
                if success:
                    logging.info(f"\n--- Konu '{topic}' Başarıyla Zamanlandı: {post_schedule_time} ---\n")
                    published_google_posts += 1 # Başarılı yayın sayısını artır
                    existing_titles.append(topic) # Gelecek kontroller için listeye ekle
                else:
                    logging.warning(f"'{topic}' konusu için içerik üretilemedi.")

            except Exception as e:
                logging.error(f"\n*** HATA: Konu '{topic}' İşlenemedi: {e} ***\n")
        
        if published_google_posts < 3:
            logging.warning(f"\n!!! UYARI: Hedeflenen 3 Google içeriği yerine sadece {published_google_posts} adet üretilebildi. Konu havuzu yetersiz olabilir.")

    logging.info("="*50)
    logging.info(f"OTOMATİK GÜNLÜK İÇERİK ÜRETİMİ TAMAMLANDI - {datetime.now()}")
    logging.info("="*50)


def simple_background_task():
    """
    Sadece log yazan ve bir dosya oluşturan en basit arka plan görevi.
    Ağ bağlantısı veya karmaşık kütüphaneler kullanmaz.
    """
    logging.info("--- SIMPLE BACKGROUND TASK STARTED ---")
    try:
        with open("test.txt", "w") as f:
            f.write(f"Hello from the background at {datetime.now()}")
        logging.info("--- TEST.TXT DOSYASI BAŞARIYLA OLUŞTURULDU ---")
    except Exception as e:
        logging.error(f"--- SIMPLE BACKGROUND TASK HATA VERDİ: {e} ---")
    logging.info("--- SIMPLE BACKGROUND TASK FINISHED ---")

@app.route('/test-background', methods=['POST'])
def test_background_endpoint():
    """
    Basit arka plan görevini test etmek için endpoint.
    """
    logging.info("Arka plan test isteği alındı. Basit görev zamanlayıcıya ekleniyor.")
    # scheduler.add_job(simple_background_task, 'date', run_date=datetime.now() + timedelta(seconds=2)) # Removed as per new_code
    trigger_thread = threading.Thread(target=simple_background_task)
    trigger_thread.start()
    return jsonify({"status": "success", "message": "Basit test görevi arka planda eklendi. 5 saniye içinde logları kontrol edin."})

@app.route('/generate-daily-content', methods=['POST'])
def generate_daily_content_endpoint():
    """
    Admin panelinden veya harici bir istek ile günlük içerik üretimini tetikler.
    İstek hemen yanıtlanır, asıl işlem arka planda bir thread'de çalışır.
    """
    logging.info("Manuel günlük içerik üretim isteği alındı. Arka planda başlatılıyor...")
    trigger_thread = threading.Thread(target=trigger_daily_content_generation)
    trigger_thread.start()
    return jsonify({"status": "success", "message": "Günlük içerik üretimi arka planda başlatıldı! Logları kontrol edin."}), 200

def scheduler_loop():
    """
    Her 60 saniyede bir saati kontrol eden ve doğru zamanda ana görevi tetikleyen
    basit ve güvenilir zamanlayıcı döngüsü.
    """
    logging.info("Sağlam Zamanlayıcı Döngüsü Başlatıldı.")
    
    # Türkiye saat dilimini ayarla
    turkey_tz = pytz.timezone('Europe/Istanbul')
    
    while True:
        # Türkiye saatini kullan
        now = datetime.now(turkey_tz)
        
        # Her 5 dakikada bir zamanlayıcının çalıştığını logla (daha sık ping için)
        if now.minute % 5 == 0 and now.second < 10:
            logging.info(f"Zamanlayıcı aktif - Şu anki zaman: {now.strftime('%H:%M:%S')} - Hedef zaman: 04:05")
        
        # Her gün 04:05'te çalıştır
        if now.hour == 4 and now.minute == 5:
            logging.info("Zaman geldi! Otomatik içerik üretimi tetikleniyor...")
            # Ana görevi ayrı bir thread'de başlat ki ana döngüyü bloklamasın
            trigger_thread = threading.Thread(target=trigger_daily_content_generation)
            trigger_thread.start()
            # Görevin aynı dakika içinde tekrar tetiklenmemesi için 61 saniye bekle
            time.sleep(61)
        else:
            # Bir sonraki kontrol için 10 saniye bekle (daha sık ping için)
            time.sleep(10)

# Zamanlayıcı döngüsünü ana uygulamadan ayrı bir thread'de başlat
scheduler_thread = threading.Thread(target=scheduler_loop)
scheduler_thread.daemon = True
scheduler_thread.start()

if __name__ == '__main__':
    # Flask uygulamasını başlat
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
