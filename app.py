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

load_dotenv()  

app = Flask(__name__)
Compress(app)  # 응답 데이터 압축 활성화 (속도 향상)

UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'weather_images')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)  # 이미지 저장 폴더가 없으면 생성

ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp'}  # 허용 확장자 목록

# --- 주소 처리 함수들 ---
def simplify_address(addr: str) -> str:
   """
   행정구역명 간략화 (예: '서울특별시' -> '서울시')
   """
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
   """
   상세주소(건물번호, 번지 등) 제거하여 간결한 주소 반환
   """
   parts = addr.split()
   filtered = []
   for p in parts:
      # 숫자나 '번지' 포함 시 상세주소로 판단하고 이후는 제외
      if re.search(r'\d', p) or '번지' in p:
         break
      filtered.append(p)
   return ' '.join(filtered)

def get_address_detail(geo_data: dict) -> str:
   """
   Google Geocode API 응답에서 의미 있는 주소 정보 추출 및 정리
   """
   if geo_data.get('status') != 'OK' or not geo_data.get('results'):
      return "주소 정보 없음"

   addr = {'country': '', 'admin1': '', 'admin2': '', 'locality': '', 'sub_locality': '', 'neighborhood': ''}
   for result in geo_data['results']:
      for comp in result.get('address_components', []):
         types = comp.get('types', [])
         name = comp.get('long_name', '')
         if 'country' in types and not addr['country']:
            addr['country'] = name
         elif 'administrative_area_level_1' in types and not addr['admin1']:
            addr['admin1'] = name
         elif 'administrative_area_level_2' in types and not addr['admin2']:
            addr['admin2'] = name
         elif 'locality' in types and not addr['locality']:
            addr['locality'] = name
         elif 'sublocality_level_1' in types and not addr['sub_locality']:
            addr['sub_locality'] = name
         elif 'neighborhood' in types and not addr['neighborhood']:
            addr['neighborhood'] = name

   parts = [addr['country'], addr['admin1'], addr['admin2']]

   # 'admin1'이 'admin2'에 포함되면 중복 제거
   if addr['admin2'] and addr['admin1'] in addr['admin2']:
      parts.pop(1)

   for extra in [addr['locality'], addr['sub_locality'], addr['neighborhood']]:
      if extra and extra not in parts:
         parts.append(extra)

   filtered = []
   for p in parts:
      if p and p not in filtered:
         filtered.append(p)
   return ' '.join(filtered)

# --- 날씨 정보 매핑 함수 ---
def map_weather_info(description: str, icon: str):
   """
   OpenWeatherMap의 영문 날씨 설명을 한글 상태와 아이콘 코드로 변환
   """
   desc_lower = description.lower()
   is_night = icon and 'n' in icon

   condition_map = {
      "맑음": ["clear sky", "few clouds", "scattered clouds"],
      "흐림": ["broken clouds", "overcast clouds"],
      "비": ["light rain", "moderate rain", "heavy intensity rain", "rain"],
      "폭우": ["very heavy rain", "extreme rain"],
      "소나기": ["light intensity shower rain", "shower rain", "heavy intensity shower rain"],
      "천둥번개": ["thunderstorm"],
      "뇌우": ["thunderstorm with light rain", "thunderstorm with rain", "thunderstorm with heavy rain"],
      "우박": ["thunderstorm with hail"],
      "눈": ["light snow", "snow"],
      "폭설": ["heavy snow"],
      "진눈깨비": ["sleet"],
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

# --- 병렬 API 호출 함수 ---
def fetch_url(url, results, key):
   """
   지정 URL을 요청하고 결과를 results 딕셔너리에 저장
   """
   try:
      resp = requests.get(url, timeout=5)
      resp.raise_for_status()
      results[key] = resp.json()
   except Exception as e:
      results[key] = {'error': str(e)}

# --- GIF 최적화를 위한 gifsicle 호출 ---
def optimize_gif_with_gifsicle(input_bytes: bytes) -> bytes:
   """
   gifsicle로 GIF 최적화 (압축, 색상 제한 등)
   """
   try:
      process = subprocess.run(
         ['gifsicle', '-O3', '--colors', '256'],
         input=input_bytes,
         stdout=subprocess.PIPE,
         stderr=subprocess.PIPE,
         check=True
      )
      return process.stdout
   except subprocess.CalledProcessError as e:
      print(f"gifsicle 최적화 실패: {e.stderr.decode()}")
      return input_bytes

# --- 이미지 저장 및 최적화 ---
def save_optimized_image(img_data: bytes, ext: str, weather: str):
   """
   업로드된 이미지 또는 URL 이미지 저장.
   기존 동일한 날씨 이름 이미지 삭제 후 저장.
   GIF는 gifsicle 최적화, SVG는 원본 저장,
   그 외는 Pillow로 썸네일 생성 후 저장.
   """
   ext_clean = ext.lstrip('.').lower()
   filename = secure_filename(f"{weather}.{ext_clean}")
   file_path = os.path.join(UPLOAD_FOLDER, filename)

   # 동일한 날씨 이름의 기존 이미지 삭제
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

# --- 캐시 저장소 및 TTL 설정 (API 호출 최소화) ---
weather_cache = {}
CACHE_TTL_SECONDS = 300  # 5분 캐시 유지

def is_cache_valid(timestamp):
   # 현재 시간과 저장 시간 차이가 TTL 이내인지 확인
   return (time.time() - timestamp) < CACHE_TTL_SECONDS

# --- Flask 라우트 ---

@app.route('/')
def root_redirect():
   # 루트 접근 시 /weather 페이지로 리다이렉트
   return redirect(url_for('weather_page_get'))

@app.route('/weather', methods=['GET'])
def weather_page_get():
   # 날씨 조회 화면 렌더링
   return render_template('weather.html')

@app.route('/weather', methods=['POST'])
def weather_view():
   # 클라이언트에서 위도, 경도 받아 날씨 및 주소 정보 응답 (캐시 적용)
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
      # 캐시된 데이터가 유효하면 재사용
      return jsonify(cached[1])

   OPENWEATHER_API_KEY = os.getenv('OPENWEATHER_API_KEY')
   MAPS_API_KEY = os.getenv('MAPS_API_KEY')  

   urls = {
      'weather': f'https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric',
      'forecast': f'https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric',
      'geo': f'https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lon}&language=ko&key={MAPS_API_KEY}'
   }

   results = {}
   # API를 병렬로 호출하여 응답 시간 단축
   threads = [threading.Thread(target=fetch_url, args=(url, results, key)) for key, url in urls.items()]
   for t in threads:
      t.start()
   for t in threads:
      t.join()

   # API 호출 에러 체크
   if any('error' in results.get(k, {}) for k in urls):
      return jsonify({'cod': 500, 'message': 'API 호출 오류'})

   weather_data = results['weather']
   forecast_data = results['forecast']
   geo_data = results['geo']

   raw_address = get_address_detail(geo_data)  # 주소 정보 추출
   cleaned_address = remove_detail_address(simplify_address(raw_address))  # 간략화 및 상세주소 제거

   desc_kr, icon_fixed = map_weather_info(
      weather_data['weather'][0]['description'],
      weather_data['weather'][0].get('icon')
   )

   # 16시간 단위 예보 생성
   hourly_forecast = []
   for item in forecast_data.get('list', [])[:26]:
      desc_hourly, icon_hourly = map_weather_info(
         item['weather'][0]['description'], item['weather'][0].get('icon')
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

   # 사용자 업로드 이미지 경로 찾기 (날씨 상태명 기반)
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

   # 결과를 캐시에 저장 (현재 시간, 데이터)
   weather_cache[cache_key] = (time.time(), response_json)

   return jsonify(response_json)

@app.route('/upload')
def upload_page():
   # 이미지 업로드 페이지 렌더링
   return render_template('upload.html')

@app.route('/upload-image', methods=['POST'])
def upload_image():
   # 사용자가 날씨 상태별 이미지를 업로드 또는 URL로 등록
   weather = request.form.get('weather')
   if not weather:
      return jsonify({'success': False, 'message': '날씨 선택이 필요합니다.'})

   file = request.files.get('image')
   image_url = request.form.get('image_url')

   if file:
      ext = os.path.splitext(file.filename)[1].lower()
      if ext not in ALLOWED_EXTENSIONS:
         return jsonify({'success': False, 'message': '허용되지 않은 파일 확장자입니다.'})
      success, msg = save_optimized_image(file.read(), ext, weather)
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
         success, msg = save_optimized_image(resp.content, ext, weather)
         if success:
            return jsonify({'success': True})
         else:
            return jsonify({'success': False, 'message': msg})
      except Exception as e:
         return jsonify({'success': False, 'message': f'이미지 다운로드 실패: {str(e)}'})

   return jsonify({'success': False, 'message': '이미지 파일 또는 URL이 필요합니다.'})

@app.route('/reset-all-images', methods=['POST'])
def reset_all_images():
   try:
      # 기본 이미지로 덮어쓰기 로직 구현 (예: 기본 이미지 복사, 삭제 등)
      # 예시: 기본 이미지 디렉토리에서 사용자 업로드 이미지 덮어쓰기 or 삭제 처리
      # 기본 이미지 경로 예: static/weather_images/default_clear.png 등

      # 예를 들어 모든 사용자 업로드 이미지 삭제:
      upload_dir = os.path.join(app.root_path, 'static', 'weather_images')
      for fname in os.listdir(upload_dir):
         fpath = os.path.join(upload_dir, fname)
         if os.path.isfile(fpath):
            os.remove(fpath)

      return jsonify(success=True)
   except Exception as e:
      return jsonify(success=False, message=str(e))

# 캐시된 정적 파일 최대 캐시 시간 설정 (1시간)
app.send_file_max_age_default = 3600

if __name__ == '__main__':
   app.run(debug=True)
