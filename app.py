from flask import Flask, render_template, request, jsonify, redirect, url_for
import os
import re
import stat
import threading
import requests
from datetime import datetime
from werkzeug.utils import secure_filename
from flask_compress import Compress
from PIL import Image
import io

app = Flask(__name__)
Compress(app)

UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'weather_images')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp'}

# --- 주소 관련 함수들 ---

def simplify_address(addr: str) -> str:
   """시도 명칭 축약 (예: 서울특별시 -> 서울시, 경기도 -> 경기)"""
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
   주소 내 숫자나 '번지'가 포함된 상세주소부터 제거하여 간략 주소 반환
   """
   parts = addr.split()
   filtered = []
   for part in parts:
      if re.search(r'\d', part) or '번지' in part:
         break
      filtered.append(part)
   return ' '.join(filtered)

def get_address_detail(geo_data: dict) -> str:
   """
   Google Geo API 결과에서 상세 주소 추출 및 간략화
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

   # admin2(구)가 admin1(시도) 포함 시 중복 제거
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

# --- 날씨 상태 매핑 함수 ---

def map_weather_info(description: str, icon: str):
   """
   OpenWeather 날씨 설명 및 아이콘코드를 한글 및 맞는 아이콘 코드로 매핑
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

   # 매핑 안된 경우 원문과 아이콘 그대로 반환
   return description, icon or "01d"

# --- 멀티 API 호출 ---

def fetch_url(url, results, key):
   try:
      resp = requests.get(url, timeout=5)  # 타임아웃 5초
      resp.raise_for_status()
      results[key] = resp.json()
   except Exception as e:
      results[key] = {'error': str(e)}

# --- 파일 권한 조절 ---

def allow_write_permission(file_path: str):
   """
   윈도우 등에서 파일에 쓰기 권한 부여 (덮어쓰기 가능)
   """
   try:
      os.chmod(file_path, stat.S_IWRITE)
      return True, None
   except Exception as e:
      return False, str(e)

def remove_all_permissions(file_path: str):
   """
   윈도우 등에서 파일 권한 모두 제거 (읽기/쓰기 모두 불가)
   """
   try:
      os.chmod(file_path, 0)
      return True, None
   except Exception as e:
      return False, str(e)

# --- 이미지 저장 및 최적화 ---

def save_optimized_image(img_data: bytes, ext: str, weather: str):
   try:
      ext_clean = ext.lstrip('.').lower()
      filename = secure_filename(f"{weather}.{ext_clean}")  # 확장자 유지
      file_path = os.path.join(UPLOAD_FOLDER, filename)

      # 기존 동일한 날씨 이름의 파일 삭제 (확장자 무관)
      for fname in os.listdir(UPLOAD_FOLDER):
         if fname.startswith(weather + "."):
            existing_path = os.path.join(UPLOAD_FOLDER, fname)
            os.remove(existing_path)

      img = Image.open(io.BytesIO(img_data))
      img.thumbnail((800, 800), Image.Resampling.LANCZOS)
      # 저장 형식은 확장자에 따라 지정 (예: PNG, JPEG, GIF 등)
      format_map = {
         'png': 'PNG',
         'jpg': 'JPEG',
         'jpeg': 'JPEG',
         'gif': 'GIF',
         'svg': 'SVG',  # PIL은 SVG 미지원, SVG는 바이너리 그대로 저장 필요 (참고)
         'webp': 'WEBP'
      }
      save_format = format_map.get(ext_clean, 'PNG')

      # SVG 파일은 PIL로 저장 불가능 → 바이너리 그대로 저장 처리
      if ext_clean == 'svg':
         with open(file_path, 'wb') as f:
            f.write(img_data)
      else:
         img.save(file_path, format=save_format, optimize=True)

      return True, None
   except Exception as e:
      return False, str(e)

# --- Flask 라우트 ---

@app.route('/')
def root_redirect():
   """루트 접속 시 /weather 페이지로 리다이렉트"""
   return redirect(url_for('weather_page_get'))

@app.route('/weather', methods=['GET'])
def weather_page_get():
   """날씨 정보 페이지 렌더링"""
   return render_template('weather.html')

@app.route('/weather', methods=['POST'])
def weather_view():
   """
   위도/경도 받아 OpenWeather, Google Geo API 호출 후 결과 JSON 반환
   """
   data = request.get_json() or {}
   lat, lon = data.get('lat'), data.get('lon')
   if not lat or not lon:
      return jsonify({'cod': 400, 'message': '위도, 경도 정보가 필요합니다.'})

   try:
      lat = round(float(lat), 5)
      lon = round(float(lon), 5)
   except ValueError:
      return jsonify({'cod': 400, 'message': '잘못된 위도/경도입니다.'})

   OPENWEATHER_API_KEY = '947036a512b635c1c71e8918b3ef8267'
   MAPS_API_KEY = 'AIzaSyBngLYp-oC51BCoXTy1PhUaj9Hm239t_98'

   urls = {
      'weather': f'https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric',
      'forecast': f'https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric',
      'geo': f'https://maps.googleapis.com/maps/api/geocode/json?latlng={lat},{lon}&language=ko&key={MAPS_API_KEY}'
   }

   results = {}
   threads = [threading.Thread(target=fetch_url, args=(url, results, key)) for key, url in urls.items()]
   for t in threads:
      t.start()
   for t in threads:
      t.join()

   if any('error' in results.get(k, {}) for k in urls):
      return jsonify({'cod': 500, 'message': 'API 호출 오류'})

   weather_data = results['weather']
   forecast_data = results['forecast']
   geo_data = results['geo']

   raw_address = get_address_detail(geo_data)
   cleaned_address = remove_detail_address(simplify_address(raw_address))

   desc_kr, icon_fixed = map_weather_info(
      weather_data['weather'][0]['description'],
      weather_data['weather'][0].get('icon')
   )

   hourly_forecast = []
   for item in forecast_data.get('list', [])[:16]:
      desc_hourly, icon_hourly = map_weather_info(
         item['weather'][0]['description'], item['weather'][0].get('icon')
      )
      hourly_forecast.append({
         'time': datetime.strptime(item['dt_txt'], "%Y-%m-%d %H:%M:%S").strftime("%H:%M"),
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
   - form-data로 'weather' 필수
   - 'image' 파일 또는 'image_url' 중 하나 선택
   - 기본 이미지 선택시 업로드 없이 성공 반환
   """
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
         content_type = resp.headers.get('Content-Type', '')
         if not content_type.startswith('image/'):
            return jsonify({'success': False, 'message': 'URL이 이미지 파일이 아닙니다.'})
         success, msg = save_optimized_image(resp.content, ext, weather)
         if success:
            return jsonify({'success': True})
         else:
            return jsonify({'success': False, 'message': msg})
      except Exception as e:
         return jsonify({'success': False, 'message': f'이미지 다운로드 실패: {str(e)}'})

   if weather == 'default':
      # 기본 이미지 선택 시 처리
      return jsonify({'success': True})

   return jsonify({'success': False, 'message': '이미지 파일 또는 URL이 필요합니다.'})

@app.route('/reset-images', methods=['POST'])
def reset_images():
   """
   업로드된 모든 이미지 삭제
   """
   try:
      for fname in os.listdir(UPLOAD_FOLDER):
         fpath = os.path.join(UPLOAD_FOLDER, fname)
         os.chmod(fpath, stat.S_IWRITE)
         os.remove(fpath)
      return jsonify({'success': True})
   except Exception as e:
      return jsonify({'success': False, 'message': str(e)})

# 정적 파일 캐싱 1시간 설정
app.send_file_max_age_default = 3600

if __name__ == '__main__':
   app.run(debug=True)
