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
      if k in addr:  # 주소에 행정구역명이 포함되어 있으면
            return addr.replace(k, v, 1)  # 첫 번째만 간략명으로 대체
   return addr  # 없으면 원본 반환


def remove_detail_address(addr: str) -> str:
   """번지, 건물번호 등 상세 주소 요소 제거"""
   parts = addr.split()  # 공백 기준 분할
   filtered = []
   for p in parts:
      # 숫자 포함 또는 '번지' 단어 나오면 주소의 상세 부분으로 판단하여 중단
      if re.search(r'\d', p) or '번지' in p:
            break
      filtered.append(p)
   # 숫자 이전까지만 합쳐서 반환
   return ' '.join(filtered)


def get_address_detail(geo_data: dict) -> str:
   """카카오 좌표→주소 API 응답에서 행정구역명만 추출"""
   documents = geo_data.get('documents', [])
   if not documents:
      return "주소 정보 없음"
   
   # 주소 또는 도로명주소 정보 가져오기
   addr_info = documents[0].get('address') or documents[0].get('road_address')
   if not addr_info:
      return "주소 정보 없음"
   
   parts = []
   # 1~3depth 행정구역명 순서대로 추가
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
   is_night = icon and 'n' in icon  # 아이콘에 'n' 포함시 야간으로 판단

   # 영어 상태명과 대응하는 한글 상태명 사전
   condition_map = {
      "맑음": ["clear sky"],
      "흐림": ["few clouds", "scattered clouds", "broken clouds", "overcast clouds"],
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

   # 아이콘 코드 기본 매핑
   icon_map = {
      "맑음": "01", "흐림": "04", "비": "10", "폭우": "10", "소나기": "09",
      "천둥번개": "11", "뇌우": "11", "우박": "11", "눈": "13", "폭설": "13",
      "진눈깨비": "13", "소낙눈": "13", "안개": "50", "황사": "50"
   }

   for key, desc_list in condition_map.items():
      if desc_lower in desc_list:  # 영어 상태명 리스트에 있으면
            suffix = 'n' if is_night else 'd'  # 아이콘 코드 접미사
            return key, f"{icon_map[key]}{suffix}"  # 한글명, 아이콘코드 반환
   
   # 매칭 실패 시 원본 description과 icon 또는 기본 "01d" 반환
   return description, icon or "01d"


# -------------------------------
# API 호출 (병렬)
# -------------------------------

def fetch_url(url, results, key):
   """스레드에서 API 호출 후 결과를 results 딕셔너리에 저장"""
   try:
      headers = {}
      if 'dapi.kakao.com' in url:
            # 카카오 API 호출 시 REST API 키를 헤더에 추가
            headers['Authorization'] = f'KakaoAK {os.getenv("KAKAO_REST_API_KEY")}'
      
      resp = requests.get(url, headers=headers, timeout=5)  # 5초 제한
      resp.raise_for_status()  # HTTP 오류 발생 시 예외
      results[key] = resp.json()
   
   except Exception as e:
      print(f"[ERROR] {key} API 호출 실패: {str(e)}")
      results[key] = {'error': str(e)}  # 실패 정보 저장


# -------------------------------
# 이미지 처리 및 저장
# -------------------------------

def optimize_gif_with_gifsicle(input_bytes: bytes) -> bytes:
   """gifsicle 유틸을 사용해 GIF 이미지를 최적화함"""
   try:
      process = subprocess.run(
            ['gifsicle', '-O3', '--colors', '256'],  # 최적화 옵션
            input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
      )
      return process.stdout  # 최적화된 이미지 바이트 반환
   
   except subprocess.CalledProcessError as e:
      print(f"gifsicle 최적화 실패: {e.stderr.decode()}")
      return input_bytes  # 실패 시 원본 반환


def save_optimized_image(img_data: bytes, ext: str, weather: str):
   """이미지를 저장 전에 최적화하고 지정 폴더에 저장"""
   ext_clean = ext.lstrip('.').lower()  # 확장자 점 제거, 소문자 변환
   filename = secure_filename(f"{weather}.{ext_clean}")  # 안전한 파일명 생성
   file_path = os.path.join(UPLOAD_FOLDER, filename)

   # 동일한 날씨 이름으로 시작하는 기존 파일 삭제 (중복 방지)
   for fname in os.listdir(UPLOAD_FOLDER):
      if fname.startswith(weather + "."):
               os.remove(os.path.join(UPLOAD_FOLDER, fname))

   try:
      if ext_clean == 'gif':
            # GIF는 gifsicle로 최적화 후 저장
            optimized_bytes = optimize_gif_with_gifsicle(img_data)
            with open(file_path, 'wb') as f:
               f.write(optimized_bytes)

      elif ext_clean == 'svg':
            # SVG는 텍스트 기반이므로 그대로 저장
            with open(file_path, 'wb') as f:
               f.write(img_data)

      else:
            # PNG, JPG, WEBP 등 일반 이미지는 Pillow로 열어 썸네일 생성 후 저장
            img = Image.open(io.BytesIO(img_data))
            img.thumbnail((800, 800), Image.Resampling.LANCZOS)  # 최대 크기 제한
            format_map = {'png': 'PNG', 'jpg': 'JPEG', 'jpeg': 'JPEG', 'webp': 'WEBP'}
            save_format = format_map.get(ext_clean, 'PNG')
            img.save(file_path, format=save_format, optimize=True)  # 최적화 저장

      return True, None  # 성공

   except Exception as e:
      return False, str(e)  # 실패 시 에러 메시지 반환


# -------------------------------
# 캐시 관련 설정
# -------------------------------

weather_cache = {}  # 캐시 딕셔너리: {(lat, lon): (timestamp, data)}
CACHE_TTL_SECONDS = 300  # 캐시 유효 시간 5분


def is_cache_valid(timestamp):
   """캐시된 데이터가 TTL 내에 있는지 확인"""
   return (time.time() - timestamp) < CACHE_TTL_SECONDS


# -------------------------------
# Flask 라우트 정의
# -------------------------------

@app.route('/')
def root_redirect():
   # 루트 접속 시 /weather 페이지로 리다이렉트
   return redirect(url_for('weather_page_get'))


@app.route('/weather', methods=['GET'])
def weather_page_get():
   # GET 요청: 날씨 페이지 렌더링 및 Kakao JavaScript API 키 전달
   kakao_js_key = os.getenv("KAKAO_JAVASCRIPT_KEY")
   return render_template('weather.html', kakao_key=kakao_js_key)


@app.route('/weather', methods=['POST'])
def weather_view():
   # POST 요청: 클라이언트에서 위도, 경도 받아 날씨, 예보, 주소 정보 제공
   data = request.get_json() or {}
   lat, lon = data.get('lat'), data.get('lon')

   if not lat or not lon:
      # 위도/경도 미전달 시 오류 응답
      return jsonify({'cod': 400, 'message': '위도, 경도 정보가 필요합니다.'})

   try:
      # 위도, 경도 소수점 5자리로 반올림
      lat, lon = round(float(lat), 5), round(float(lon), 5)
   except ValueError:
      # 변환 실패 시 오류 반환
      return jsonify({'cod': 400, 'message': '잘못된 위도/경도입니다.'})

   cache_key = (lat, lon)
   cached = weather_cache.get(cache_key)
   if cached and is_cache_valid(cached[0]):
      # 유효한 캐시가 있으면 캐시된 결과 반환
      return jsonify(cached[1])

   OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

   # 호출할 API URL 목록 준비
   urls = {
      'weather': f'https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric',
      'forecast': f'https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric',
      'geo': f'https://dapi.kakao.com/v2/local/geo/coord2address.json?x={lon}&y={lat}'
   }

   results = {}

   # 각 API를 별도 스레드로 병렬 호출
   threads = [threading.Thread(target=fetch_url, args=(url, results, key)) for key, url in urls.items()]
   for t in threads:
      t.start()
   for t in threads:
      t.join()

   # 호출 중 에러가 있으면 에러 반환
   if any('error' in results.get(k, {}) for k in urls):
      return jsonify({
            'cod': 500,
            'message': 'API 호출 오류',
            'details': {k: results[k].get('error') for k in urls if 'error' in results.get(k, {})}
      })

   weather_data = results['weather']

   # 한국 이외 국가일 경우 지원하지 않음 처리
   if weather_data.get('sys', {}).get('country') != 'KR':
      return jsonify({'cod': 403, 'message': '해당 위치는 지원하지 않는 지역입니다.'})

   forecast_data = results.get('forecast', {})
   geo_data = results.get('geo', {})

   # 카카오 API 결과로부터 행정주소 획득 및 간략화
   raw_address = get_address_detail(geo_data)
   cleaned_address = remove_detail_address(simplify_address(raw_address))

   # 현재 날씨 한글명과 아이콘 코드 변환
   desc_kr, icon_fixed = map_weather_info(
      weather_data['weather'][0]['description'],
      weather_data['weather'][0].get('icon')
   )

   # 시간별 예보 리스트 작성 (최대 26개)
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

   # 사용자가 업로드한 날씨별 이미지가 있으면 URL 생성
   user_img_url = None
   base_name = weather_data['weather'][0]['main'].lower()
   for ext in ALLOWED_EXTENSIONS:
      img_path = os.path.join(UPLOAD_FOLDER, f"{base_name}{ext}")
      if os.path.isfile(img_path):
            user_img_url = f"/static/weather_images/{base_name}{ext}"
            break

   # 최종 응답 JSON 구조 준비
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

   # 결과를 캐시에 저장 (타임스탬프 포함)
   weather_cache[cache_key] = (time.time(), response_json)
   return jsonify(response_json)


@app.route('/upload')
def upload_page():
   # 이미지 업로드 페이지 렌더링
   return render_template('upload.html')


@app.route('/upload-image', methods=['POST'])
def upload_image():
   # 이미지 업로드 처리 (파일 업로드 또는 URL)
   weather = request.form.get('weather')
   if not weather:
      return jsonify({'success': False, 'message': '날씨 선택이 필요합니다.'})

   weather = weather.lower()
   file = request.files.get('image')
   image_url = request.form.get('image_url')

   if file:
      ext = os.path.splitext(file.filename)[1].lower()
      if ext not in ALLOWED_EXTENSIONS:
            return jsonify({'success': False, 'message': '허용되지 않은 파일 확장자입니다.'})
      
      # 파일 내용 읽어 최적화 저장 함수 호출
      success, msg = save_optimized_image(file.read(), ext, weather)
      return jsonify({'success': success, 'message': msg})

   if image_url:
      url_clean = image_url.strip()
      base_url = url_clean.split('?')[0]  # 쿼리 제거
      
      # URL 끝 확장자가 이미지인지 검사
      match = re.search(r'\.(png|jpg|jpeg|gif|svg|webp)$', base_url, re.IGNORECASE)
      if not match:
            return jsonify({'success': False, 'message': '지원되지 않는 이미지 URL입니다.'})
      
      ext = '.' + match.group(1).lower()
      try:
            # URL에서 이미지 다운로드 시도
            resp = requests.get(url_clean, timeout=10)
            resp.raise_for_status()
            
            # HTTP Content-Type이 이미지여야 통과
            if not resp.headers.get('Content-Type', '').startswith('image/'):
               return jsonify({'success': False, 'message': 'URL이 이미지 파일이 아닙니다.'})
            
            # 다운로드 받은 이미지 데이터 저장 시도
            success, msg = save_optimized_image(resp.content, ext, weather)
            return jsonify({'success': success, 'message': msg})
      
      except Exception as e:
            return jsonify({'success': False, 'message': f'이미지 다운로드 실패: {str(e)}'})

   # 파일도 URL도 없으면 오류
   return jsonify({'success': False, 'message': '이미지 파일 또는 URL이 필요합니다.'})


@app.route('/reset-all-images', methods=['POST'])
def reset_all_images():
   # 저장된 모든 사용자 이미지 삭제 API
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
   # Google Geolocation API 호출 (IP/와이파이 등으로 위치 추정)
   GOOGLE_API_KEY = os.getenv("GOOGLE_GEOLOCATION_API_KEY")  # .env에 저장 필요
   try:
      url = f"https://www.googleapis.com/geolocation/v1/geolocate?key={GOOGLE_API_KEY}"
      resp = requests.post(url, timeout=5)
      resp.raise_for_status()
      return jsonify(resp.json())  # 위치 정보 JSON 반환
   
   except Exception as e:
      return jsonify({'error': str(e)}), 500


# -------------------------------
# Flask 앱 기본 설정
# -------------------------------

# 정적 파일 캐싱 최대 1시간으로 설정
app.send_file_max_age_default = 3600


# 개발용 직접 실행 모드
if __name__ == '__main__':
   app.run(debug=True)
