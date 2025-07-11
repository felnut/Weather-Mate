from flask import Flask, render_template, request, jsonify, redirect, url_for
import os
import requests
import re
from datetime import datetime
from werkzeug.utils import secure_filename
from flask_compress import Compress
import threading
from PIL import Image
import io
import stat

# Flask 앱 초기화 및 gzip 압축 활성화
app = Flask(__name__)
Compress(app)

# 업로드 이미지 저장 폴더 경로 및 생성
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'weather_images')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# 허용 이미지 확장자 집합
ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp'}

def allowed_file(filename):
   """파일명 확장자가 허용된 형식인지 검사"""
   return filename.lower().endswith(tuple(ALLOWED_EXTENSIONS))

# ----------------------------- [주소 처리 함수들] -----------------------------

def simplify_address(addr):
   """
   주소 문자열에서 시/도 등의 명칭을 간략화
   예: 서울특별시 -> 서울시, 경기도 -> 경기
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

def remove_detail_address(addr):
   """
   주소에서 건물 번호 등 상세주소를 제거하여 간략 주소 반환
   숫자 혹은 '번지'가 포함된 부분부터 자름
   """
   parts = addr.split()
   filtered = []
   for part in parts:
      if re.search(r'\d', part) or '번지' in part:
         break
      filtered.append(part)
   return ' '.join(filtered)

def get_address_detail(geo_data):
   """
   구글 지도 API 응답에서 주소 컴포넌트를 추출해 적절한 주소 문자열 반환
   """
   if geo_data.get('status') != 'OK' or not geo_data.get('results'):
      return "주소 정보 없음"

   comp = geo_data['results'][0].get('address_components', [])
   addr = {'country':'', 'admin1':'', 'admin2':'', 'sub_locality':'', 'locality':'', 'neighborhood':''}

   for c in comp:
      types = c.get('types', [])
      name = c.get('long_name', '')
      if 'country' in types:
         addr['country'] = name
      elif 'administrative_area_level_1' in types:
         addr['admin1'] = name
      elif 'administrative_area_level_2' in types:
         addr['admin2'] = name
      elif 'sublocality_level_1' in types:
         addr['sub_locality'] = name
      elif 'locality' in types:
         addr['locality'] = name
      elif 'neighborhood' in types:
         addr['neighborhood'] = name

   # 우선순위에 따라 주소 부분 합침
   parts = [addr['country'], addr['admin1'], addr['admin2']]
   if addr['sub_locality']:
      parts.append(addr['sub_locality'])
   elif addr['neighborhood']:
      parts.append(addr['neighborhood'])
   elif addr['locality']:
      parts.append(addr['locality'])

   return ' '.join(filter(None, parts))

# ---------------------------- [날씨 매핑 함수] ----------------------------

def map_weather_info(desc, icon):
   """
   OpenWeather API에서 받은 description과 icon을 한글 설명 및 아이콘 코드로 매핑
   """
   desc = desc.lower()
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

   for k, desc_list in condition_map.items():
      if desc in desc_list:
         suffix = 'n' if is_night else 'd'
         return k, f"{icon_map[k]}{suffix}"

   # 매핑 없으면 원문 및 기본 아이콘 반환
   return desc, icon or "01d"

# ----------------------------- [멀티 API 호출] -----------------------------

def fetch_url(url, results, key):
   """별도 스레드에서 API 호출 후 결과 저장"""
   try:
      resp = requests.get(url)
      resp.raise_for_status()
      results[key] = resp.json()
   except Exception as e:
      results[key] = {'error': str(e)}

# ----------------------------- [권한 설정 함수들] -----------------------------

def allow_write_permission(file_path):
   """
   윈도우에서 파일에 쓰기 권한 부여 (덮어쓰기 가능)
   """
   try:
      os.chmod(file_path, stat.S_IWRITE)
      return True, None
   except Exception as e:
      return False, str(e)

def remove_all_permissions(file_path):
   """
   윈도우에서 파일 권한 모두 제거 (읽기/쓰기 모두 불가)
   """
   try:
      os.chmod(file_path, 0)  # 권한 0으로 설정 시도
      return True, None
   except Exception as e:
      return False, str(e)

# ----------------------------- [이미지 저장 함수] -----------------------------

def save_optimized_image(img_data, ext, weather):
   try:
      ext_clean = ext.lstrip('.').lower()
      filename = secure_filename(f"{weather}.{ext_clean}")
      file_path = os.path.join(UPLOAD_FOLDER, filename)

      # 기존 이미지 권한 열고 삭제
      for fname in os.listdir(UPLOAD_FOLDER):
         name, extension = os.path.splitext(fname)
         if name == weather and extension.lower() in ALLOWED_EXTENSIONS:
            existing_path = os.path.join(UPLOAD_FOLDER, fname)
            allow_write_permission(existing_path)
            os.remove(existing_path)

      if ext_clean == 'svg':
         with open(file_path, 'wb') as f:
            f.write(img_data)
      else:
         img = Image.open(io.BytesIO(img_data))
         img.thumbnail((800, 800), Image.Resampling.LANCZOS)
         if ext_clean == 'gif':
            img.save(file_path, save_all=True, loop=0, optimize=True)
         else:
            save_format = 'JPEG' if ext_clean in ('jpg', 'jpeg') else ext_clean.upper()
            # progressive=True 추가 (JPEG 최적화)
            img.save(file_path, save_format, quality=85, optimize=True, progressive=True)

      success, msg = remove_all_permissions(file_path)
      if not success:
         return False, msg
      return True, None

   except Exception as e:
      return False, str(e)

# ----------------------------- [라우터들 정의] -----------------------------

@app.route('/')
def root_redirect():
   """루트 접속 시 날씨 페이지로 리다이렉트"""
   return redirect(url_for('weather_page_get'))

@app.route('/weather', methods=['GET'])
def weather_page_get():
   """날씨 페이지 렌더링 (GET)"""
   return render_template('weather.html')

@app.route('/weather', methods=['POST'])
def weather_view():
   """날씨 정보 API: 위도/경도 받아 OpenWeather, Google Geo API 호출 후 결과 반환"""
   data = request.get_json() or {}
   lat, lon = data.get('lat'), data.get('lon')
   if not lat or not lon:
      return jsonify({'cod': 400, 'message': '위도, 경도 정보가 필요합니다.'})

   try:
      lat = round(float(lat), 5)
      lon = round(float(lon), 5)
   except:
      return jsonify({'cod': 400, 'message': '잘못된 위도/경도입니다.'})

   OPENWEATHER_API_KEY = '947036a512b635c1c71e8918b3ef8267'
   MAPS_API_KEY = 'AIzaSyBngLYp-oC51BCoXTy1PhUaj9Hm239t_98'

   urls = {
      'weather': f'https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric',
      'forecast': f'https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric',
      'geo': f'https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lon}&language=ko&key={MAPS_API_KEY}'
   }

   # 멀티스레드로 API 동시 호출
   results = {}
   threads = [threading.Thread(target=fetch_url, args=(url, results, key)) for key, url in urls.items()]
   for t in threads: t.start()
   for t in threads: t.join()

   # 호출 오류 처리
   if any('error' in results.get(k, {}) for k in urls):
      return jsonify({'cod': 500, 'message': 'API 호출 오류'})

   weather_data, forecast_data, geo_data = results['weather'], results['forecast'], results['geo']

   # 주소 간략화 및 상세주소 제거
   raw_address = get_address_detail(geo_data)
   cleaned_address = remove_detail_address(simplify_address(raw_address))

   # 현재 날씨 한글 및 아이콘 매핑
   desc_kr, icon_fixed = map_weather_info(
      weather_data['weather'][0]['description'], weather_data['weather'][0].get('icon')
   )

   # 시간별 예보 (최대 16개, 약 48시간)
   hourly_forecast = []
   for item in forecast_data.get('list', [])[:16]:
      hourly_forecast.append({
      'time': datetime.strptime(item['dt_txt'], "%Y-%m-%d %H:%M:%S").strftime("%H:%M"),
      'temp': round(item['main']['temp']),
      'humidity': round(item['main']['humidity']),
      'description': map_weather_info(item['weather'][0]['description'], item['weather'][0].get('icon'))[0],
      'icon': map_weather_info(item['weather'][0]['description'], item['weather'][0].get('icon'))[1]
   })

   # 사용자 업로드 이미지 탐색 (weather 상태명 기반 파일명)
   user_img_url = None
   base_name = weather_data['weather'][0]['main'].lower()
   for ext in ALLOWED_EXTENSIONS:
      img_path = os.path.join(UPLOAD_FOLDER, f"{base_name}{ext}")
      if os.path.isfile(img_path):
         user_img_url = f"/static/weather_images/{base_name}{ext}"
         break

   # 결과 JSON 반환
   return jsonify({
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
   })

@app.route('/upload')
def upload_page():
   """이미지 업로드 페이지 렌더링"""
   return render_template('upload.html')

@app.route('/upload-image', methods=['POST'])
def upload_image():
   """
   이미지 업로드 처리
   - form-data로 'weather', 'image' 또는 'image_url' 수신
   - 이미지 URL은 다운로드하여 저장
   - 기본 이미지 선택 시 업로드 없이 성공 처리
   """
   weather = request.form.get('weather')
   if not weather:
      return jsonify({'success': False, 'message': '날씨 선택이 필요합니다.'})

   file = request.files.get('image')
   image_url = request.form.get('image_url')

   # 1. 파일 업로드 처리
   if file:
      ext = os.path.splitext(file.filename)[1].lower()
      if ext not in ALLOWED_EXTENSIONS:
         return jsonify({'success': False, 'message': '허용되지 않은 파일 확장자입니다.'})
      success, msg = save_optimized_image(file.read(), ext, weather)
      return jsonify({'success': success, 'message': msg})

   # 2. 이미지 URL 다운로드 처리
   if image_url:
      raw_url = image_url.strip()
      base_url = raw_url.split('?')[0]  # 쿼리스트링 제거

      # 확장자 추출 및 검증
      match = re.search(r'\.(png|jpg|jpeg|gif|svg|webp)$', base_url, re.IGNORECASE)
      if not match:
         return jsonify({'success': False, 'message': '지원되지 않는 이미지 URL입니다. PNG, JPG, JPEG, GIF, SVG, WEBP 형식만 가능합니다.'})
      ext = '.' + match.group(1).lower()

      try:
         resp = requests.get(raw_url, timeout=10)
         resp.raise_for_status()

         content_type = resp.headers.get('Content-Type', '')
         if not content_type.startswith('image/'):
            return jsonify({'success': False, 'message': 'URL이 이미지 파일을 가리키지 않습니다.'})

         img_data = resp.content
         success, message = save_optimized_image(img_data, ext, weather)
         if success:
            return jsonify({'success': True})
         else:
            return jsonify({'success': False, 'message': message})
      except Exception as e:
         return jsonify({'success': False, 'message': f'이미지 다운로드 실패: {str(e)}'})

   # 3. 기본(default) 날씨 이미지 처리 시 (업로드 없이)
   if weather == 'default':
      return jsonify({'success': True})

   # 4. 기타 오류
   return jsonify({'success': False, 'message': '이미지 파일 또는 URL이 필요합니다.'})

@app.route('/reset-images', methods=['POST'])
def reset_images():
   try:
      files = os.listdir(UPLOAD_FOLDER)
      for fname in files:
         fpath = os.path.join(UPLOAD_FOLDER, fname)
         # 삭제 전 권한 열기
         os.chmod(fpath, stat.S_IWRITE)
         os.remove(fpath)
      return jsonify({'success': True})
   except Exception as e:
      return jsonify({'success': False, 'message': str(e)})

# 정적 파일 캐싱 시간 (초) 설정 - 1시간
app.send_file_max_age_default = 3600

if __name__ == '__main__':
   app.run(debug=True)
