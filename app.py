from flask import Flask, render_template, request, jsonify, redirect, url_for
import os
import re
import stat
import threading
import requests
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from flask_compress import Compress
from PIL import Image
import io
import subprocess
import time
from dotenv import load_dotenv

# 환경 변수 로드 및 Flask 앱 초기화
load_dotenv()  

app = Flask(__name__)  
Compress(app)  # HTTP 응답 압축 활성화

# 업로드할 이미지 저장 폴더 경로 설정 및 폴더가 없으면 생성
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'weather_images')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 허용할 이미지 확장자 집합
ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp'}

# -------------------------------
# 한글 → 영어 상태명 전역 딕셔너리
# -------------------------------
KOR_TO_ENG_WEATHER = {
   "맑음": "Clear",
   "흐림": "Clouds",
   "비": "Rain",
   "폭우": "Rain",
   "소나기": "Rain",
   "천둥번개": "Thunderstorm",
   "뇌우": "Thunderstorm",
   "우박": "Thunderstorm",
   "눈": "Snow",
   "폭설": "Snow",
   "진눈깨비": "Snow",
   "소낙눈": "Snow",
   "안개": "Mist",
   "황사": "Dust"
}

# -------------------------------
# 주소 처리 관련 함수
# -------------------------------

def simplify_address(addr: str) -> str:
   """행정구역명을 간략화 (예: 서울특별시 → 서울시)"""
   replacements = {
      '서울특별시': '서울시', '부산광역시': '부산시', '대구광역시': '대구시',
      '인천광역시': '인천시', '광주광역시': '광주시', '대전광역시': '대전시',
      '울산광역시': '울산시', '세종특별자치시': '세종시', '경기도': '경기',
      '강원도': '강원', '충청북도': '충북', '충청남도': '충남',
      '전라북도': '전북', '전라남도': '전남', '경상북도': '경북',
      '경상남도': '경남', '제주특별자치도': '제주'
   }
   for k, v in replacements.items():
      if k in addr:
         return addr.replace(k, v, 1)
   return addr

def remove_detail_address(addr: str) -> str:
   """번지, 건물번호 등 상세 주소 요소 제거"""
   parts = addr.split()
   filtered = []
   for p in parts:
      if re.search(r'\d', p) or '번지' in p:
         break
      filtered.append(p)
   return ' '.join(filtered)

def get_address_detail(geo_data: dict) -> str:
   """카카오 좌표→주소 API 응답에서 행정구역명만 추출"""
   documents = geo_data.get('documents', [])
   if not documents:
      return "주소 정보 없음"
   
   addr_info = documents[0].get('address') or documents[0].get('road_address')
   if not addr_info:
      return "주소 정보 없음"
   
   parts = []
   if addr_info.get('region_1depth_name'):
      parts.append(addr_info['region_1depth_name'])
   if addr_info.get('region_2depth_name'):
      parts.append(addr_info['region_2depth_name'])
   if addr_info.get('region_3depth_name'):
      parts.append(addr_info['region_3depth_name'])
   return ' '.join(parts)

# -------------------------------
# 날씨 상태 매핑 함수
# -------------------------------

def map_weather_info(description: str, icon: str):
   """OpenWeatherMap 날씨 상태를 한글명과 아이콘 코드로 변환"""
   desc_lower = description.lower()
   is_night = icon and 'n' in icon

   condition_map = {
      "맑음": ["clear sky"],
      "흐림": ["few clouds", "scattered clouds", "broken clouds", "overcast clouds"],
      "비": ["light rain", "moderate rain", "heavy intensity rain", "rain"],
      "폭우": ["very heavy rain", "extreme rain"],
      "소나기": ["light intensity shower rain", "shower rain", "heavy intensity shower rain"],
      "천둥번개": ["thunderstorm"],
      "뇌우": ["thunderstorm with light rain", "thunderstorm with rain", "thunderstorm with heavy rain"],
      "우박": ["thunderstorm with hail"],
      "눈": ["light snow", "snow", "sleet"],
      "폭설": ["heavy snow"],
      "소낙눈": ["light shower snow", "shower snow"],
      "안개": ["mist", "smoke", "haze", "fog"],
      "황사": ["sand"]
   }

   icon_map = {
      "맑음": "01", "흐림": "04", "비": "10", "폭우": "10", "소나기": "09",
      "천둥번개": "11", "뇌우": "11", "우박": "11", "눈": "13", "폭설": "13",
      "진눈깨비": "13", "소낙눈": "13", "안개": "50", "황사": "50"
   }

   for key, desc_list in condition_map.items():
      if desc_lower in desc_list:
         suffix = 'n' if is_night else 'd'
         return key, f"{icon_map[key]}{suffix}"

   return description, icon or "01d"

# -------------------------------
# API 호출 (병렬)
# -------------------------------

def fetch_url(url, results, key):
   """스레드에서 API 호출 후 결과를 results 딕셔너리에 저장"""
   try:
      headers = {}
      if 'dapi.kakao.com' in url:
         headers['Authorization'] = f'KakaoAK {os.getenv("KAKAO_REST_API_KEY")}'
      
      resp = requests.get(url, headers=headers, timeout=5)
      resp.raise_for_status()
      results[key] = resp.json()
   
   except Exception as e:
      print(f"[ERROR] {key} API 호출 실패: {str(e)}")
      results[key] = {'error': str(e)}

# -------------------------------
# 이미지 처리 및 저장
# -------------------------------

def optimize_gif_with_gifsicle(input_bytes: bytes) -> bytes:
   try:
      process = subprocess.run(
         ['gifsicle', '-O3', '--colors', '256'],
         input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
      )
      return process.stdout
   except subprocess.CalledProcessError as e:
      print(f"gifsicle 최적화 실패: {e.stderr.decode()}")
      return input_bytes

def save_optimized_image(img_data: bytes, ext: str, weather: str):
   ext_clean = ext.lstrip('.').lower()
   filename = secure_filename(f"{weather}.{ext_clean}")
   file_path = os.path.join(UPLOAD_FOLDER, filename)

   for fname in os.listdir(UPLOAD_FOLDER):
      if fname.startswith(weather + "."):
         os.remove(os.path.join(UPLOAD_FOLDER, fname))

   try:
      if ext_clean == 'gif':
         optimized_bytes = optimize_gif_with_gifsicle(img_data)
         with open(file_path, 'wb') as f:
            f.write(optimized_bytes)

      elif ext_clean == 'svg':
         with open(file_path, 'wb') as f:
            f.write(img_data)

      else:
         img = Image.open(io.BytesIO(img_data))
         img.thumbnail((800, 800), Image.Resampling.LANCZOS)
         format_map = {'png': 'PNG', 'jpg': 'JPEG', 'jpeg': 'JPEG', 'webp': 'WEBP'}
         save_format = format_map.get(ext_clean, 'PNG')
         img.save(file_path, format=save_format, optimize=True)

      return True, None

   except Exception as e:
      return False, str(e)

# -------------------------------
# 캐시 관련 설정
# -------------------------------

weather_cache = {}
CACHE_TTL_SECONDS = 300

def is_cache_valid(timestamp):
   return (time.time() - timestamp) < CACHE_TTL_SECONDS

# -------------------------------
# Flask 라우트 정의
# -------------------------------

@app.route('/')
def root_redirect():
   return redirect(url_for('weather_page_get'))

@app.route('/weather', methods=['GET'])
def weather_page_get():
   kakao_js_key = os.getenv("KAKAO_JAVASCRIPT_KEY")
   return render_template('weather.html', kakao_key=kakao_js_key)

@app.route('/weather', methods=['POST'])
def weather_view():
   data = request.get_json() or {}
   lat, lon = data.get('lat'), data.get('lon')

   if not lat or not lon:
      return jsonify({'cod': 400, 'message': '위도, 경도 정보가 필요합니다.'})

   try:
      lat, lon = round(float(lat), 5), round(float(lon), 5)
   except ValueError:
      return jsonify({'cod': 400, 'message': '잘못된 위도/경도입니다.'})

   cache_key = (lat, lon)
   cached = weather_cache.get(cache_key)
   if cached and is_cache_valid(cached[0]):
      return jsonify(cached[1])

   OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

   urls = {
      'weather': f'https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric',
      'forecast': f'https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric',
      'geo': f'https://dapi.kakao.com/v2/local/geo/coord2address.json?x={lon}&y={lat}'
   }

   results = {}

   threads = [threading.Thread(target=fetch_url, args=(url, results, key)) for key, url in urls.items()]
   for t in threads:
      t.start()
   for t in threads:
      t.join()

   if any('error' in results.get(k, {}) for k in urls):
      return jsonify({
            'cod': 500,
            'message': 'API 호출 오류',
            'details': {k: results[k].get('error') for k in urls if 'error' in results.get(k, {})}
      })

   weather_data = results['weather']
   base_name = weather_data['weather'][0]['main'].lower()

   if weather_data.get('sys', {}).get('country') != 'KR':
      return jsonify({'cod': 403, 'message': '해당 위치는 지원하지 않는 지역입니다.'})
      

   forecast_data = results.get('forecast', {})
   geo_data = results.get('geo', {})

   raw_address = get_address_detail(geo_data)
   cleaned_address = remove_detail_address(simplify_address(raw_address))

   desc_kr, icon_fixed = map_weather_info(
      weather_data['weather'][0]['description'],
      weather_data['weather'][0].get('icon')
   )

   hourly_forecast = []
   for item in forecast_data.get('list', [])[:26]:
      desc_hourly, icon_hourly = map_weather_info(
         item['weather'][0]['description'],
         item['weather'][0].get('icon')
      )
      dt = datetime.strptime(item['dt_txt'], "%Y-%m-%d %H:%M:%S")
      hourly_forecast.append({
         'date': dt.strftime("%Y-%m-%d"),
         'time': dt.strftime("%H:%M"),
         'temp': round(item['main']['temp']),
         'humidity': round(item['main']['humidity']),
         'description': desc_hourly,
         'icon': icon_hourly
      })

   user_img_url = None
   base_name = weather_data['weather'][0]['main'].lower()
   for ext in ALLOWED_EXTENSIONS:
      img_path = os.path.join(UPLOAD_FOLDER, f"{base_name}{ext}")
      if os.path.isfile(img_path):
         user_img_url = f"/static/weather_images/{base_name}{ext}"
         break

   response_json = {
      'cod': 200,
      'korean_address': cleaned_address,
      'country': '대한민국',
      'current_weather': {
         'description': desc_kr,
         'icon': icon_fixed,
         'temp': round(weather_data['main']['temp']),
         'humidity': round(weather_data['main']['humidity']),
         'feels_like': round(weather_data['main'].get('feels_like', 0))
      },
      'user_weather_image': user_img_url,
      'forecast': hourly_forecast
   }

   weather_cache[cache_key] = (time.time(), response_json)
   return jsonify(response_json)

@app.route('/upload')
def upload_page():
   return render_template('upload.html')

@app.route('/upload-image', methods=['POST'])
def upload_image():
   weather = request.form.get('weather')
   if not weather:
      return jsonify({'success': False, 'message': '날씨 선택이 필요합니다.'})

   weather_eng = KOR_TO_ENG_WEATHER.get(weather, weather).lower()

   file = request.files.get('image')
   image_url = request.form.get('image_url')

   if file:
      ext = os.path.splitext(file.filename)[1].lower()
      if ext not in ALLOWED_EXTENSIONS:
         return jsonify({'success': False, 'message': '허용되지 않은 파일 확장자입니다.'})

      success, msg = save_optimized_image(file.read(), ext, weather_eng)
      return jsonify({'success': success, 'message': msg})

   if image_url:
      url_clean = image_url.strip()
      base_url = url_clean.split('?')[0]
      
      match = re.search(r'\.(png|jpg|jpeg|gif|svg|webp)$', base_url, re.IGNORECASE)
      if not match:
         return jsonify({'success': False, 'message': '지원되지 않는 이미지 URL입니다.'})
      
      ext = '.' + match.group(1).lower()
      try:
         resp = requests.get(url_clean, timeout=10)
         resp.raise_for_status()
         
         if not resp.headers.get('Content-Type', '').startswith('image/'):
            return jsonify({'success': False, 'message': 'URL이 이미지 파일이 아닙니다.'})
         
         success, msg = save_optimized_image(resp.content, ext, weather_eng)
         return jsonify({'success': success, 'message': msg})
      
      except Exception as e:
         return jsonify({'success': False, 'message': f'이미지 다운로드 실패: {str(e)}'})

   return jsonify({'success': False, 'message': '이미지 파일 또는 URL이 필요합니다.'})

@app.route('/reset-all-images', methods=['POST'])
def reset_all_images():
   try:
      upload_dir = os.path.join(app.root_path, 'static', 'weather_images')
      for fname in os.listdir(upload_dir):
         fpath = os.path.join(upload_dir, fname)
         if os.path.isfile(fpath):
            os.remove(fpath)
      return jsonify(success=True)
   
   except Exception as e:
      return jsonify(success=False, message=str(e))

@app.route('/geolocate', methods=['POST'])
def geolocate_by_google():
   GOOGLE_API_KEY = os.getenv("GOOGLE_GEOLOCATION_API_KEY")
   try:
      url = f"https://www.googleapis.com/geolocation/v1/geolocate?key={GOOGLE_API_KEY}"
      resp = requests.post(url, timeout=5)
      resp.raise_for_status()
      return jsonify(resp.json())
   
   except Exception as e:
      return jsonify({'error': str(e)}), 500

# -------------------------------
# Flask 앱 기본 설정
# -------------------------------

app.send_file_max_age_default = 3600

if __name__ == '__main__':
   app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
