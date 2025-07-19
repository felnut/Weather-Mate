from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory
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
import glob

# 환경 변수 로드 및 Flask 앱 초기화
load_dotenv()

# 사용자 이미지를 저장할 새로운 폴더 경로 설정
# 현재 스크립트 파일이 있는 디렉토리의 'data/images' 폴더를 사용합니다.
USER_IMAGES_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'images')

# Flask 앱 초기화 시, 기본 static 폴더는 그대로 사용
app = Flask(__name__, static_folder='static')
Compress(app) # HTTP 응답 압축 활성화

# 사용자 이미지 폴더에 대한 정적 파일 서비스 설정 (새로 추가/수정)
# /user_images/ 경로로 요청이 오면 USER_IMAGES_FOLDER에서 파일을 찾음
@app.route('/user_images/<filename>')
def uploaded_file(filename):
    return send_from_directory(USER_IMAGES_FOLDER, filename)


# 폴더가 없으면 생성 (애플리케이션 시작 시 한 번 실행)
os.makedirs(USER_IMAGES_FOLDER, exist_ok=True)

# 허용할 이미지 확장자 집합
ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp'}

# -------------------------------
# 한글 → 영어 상태명 전역 딕셔너리 (기존 코드)
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
# 주소 처리 관련 함수 (기존 코드)
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
# 날씨 상태 매핑 함수 (기존 코드)
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
      "진눈깨비": ["light shower snow", "shower snow"],
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
# API 호출 (병렬) (기존 코드)
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
# 이미지 처리 및 저장 (수정)
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
        # 최적화 실패 시 원본 바이트 반환
        return input_bytes 

def save_optimized_image(image_data: bytes, weather_condition: str, file_extension: str) -> str:
    """
    주어진 이미지 데이터를 최적화하여 저장하고, 저장된 파일의 이름을 반환합니다.
    Args:
        image_data: 이미지의 바이너리 데이터.
        weather_condition: 날씨 조건 (예: 'clear', 'rain').
        file_extension: 원본 파일의 확장자 (예: '.png', '.jpg').
    Returns:
        저장된 파일의 이름 (확장자 포함).
    """
    try:
        # 확장자가 없거나 허용되지 않으면 기본값으로 .png 설정
        if not file_extension or file_extension.lower() not in ALLOWED_EXTENSIONS:
            print(f"경고: 유효하지 않거나 없는 확장자 '{file_extension}'. '.png'로 대체합니다.")
            file_extension = '.png'
        
        # 파일명은 '날씨조건.확장자' 형태로 만듭니다. (확장자 포함)
        # secure_filename으로 한 번 더 처리하여 안전한 파일명 생성
        filename_base = secure_filename(weather_condition.lower())
        filename = f"{filename_base}{file_extension.lower()}"
        filepath = os.path.join(USER_IMAGES_FOLDER, filename)

        # 해당 날씨 조건으로 시작하는 기존의 모든 확장자 파일을 삭제
        for fname in glob.glob(os.path.join(USER_IMAGES_FOLDER, f"{filename_base}.*")):
            if os.path.isfile(fname):
                os.remove(fname)

        if file_extension.lower() == '.gif':
            # GIF는 gifsicle로 최적화 시도
            optimized_data = optimize_gif_with_gifsicle(image_data)
            with open(filepath, 'wb') as f:
                f.write(optimized_data)
        elif file_extension.lower() == '.svg':
            # SVG는 PIL로 처리하기 어려우므로 원본 저장
            with open(filepath, 'wb') as f:
                f.write(image_data)
        else:
            # 기타 이미지 파일은 PIL로 최적화
            img = Image.open(io.BytesIO(image_data))
            # JPG로 저장할 때 RGBA 모드 이미지는 RGB로 변환 (투명도 정보 손실)
            if img.mode == 'RGBA' and file_extension.lower() == '.jpg':
                img = img.convert('RGB')
            # 이미지 크기 조절 (예: 최대 800x800)
            img.thumbnail((800, 800), Image.Resampling.LANCZOS)
            # 파일 형식에 따라 저장
            format_map = {
                '.png': 'PNG', 
                '.jpg': 'JPEG', 
                '.jpeg': 'JPEG', 
                '.webp': 'WEBP'
            }
            save_format = format_map.get(file_extension.lower(), 'PNG')
            img.save(filepath, format=save_format, optimize=True, quality=85) # JPG/PNG 최적화

        # 파일 권한 설정 (읽기 가능하게)
        os.chmod(filepath, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        print(f"이미지 저장 성공: {filepath}")
        return filename # 저장된 파일의 최종 이름 (확장자 포함) 반환

    except Exception as e:
        print(f"이미지 저장 및 최적화 중 오류 발생: {e}")
        return None

# -------------------------------
# 캐시 관련 설정 (기존 코드)
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
   # `base_name`을 날씨 영어 조건으로 변경
   main_weather_english = weather_data['weather'][0]['main'].lower()

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

   # 사용자 업로드 이미지 확인 및 URL 구성 (수정)
   user_img_url = None
   # `glob`을 사용하여 해당 날씨 조건으로 시작하는 모든 확장자의 파일을 찾습니다.
   user_image_path_pattern = os.path.join(USER_IMAGES_FOLDER, f"{main_weather_english}.*") 
   found_user_images = glob.glob(user_image_path_pattern)
   
   if found_user_images:
       # 사용자 이미지가 존재하면, 첫 번째로 찾은 파일의 URL을 사용합니다.
       # Flask의 `uploaded_file` 엔드포인트를 사용하여 '/user_images/<filename>' 경로를 생성합니다.
       final_image_filename = os.path.basename(found_user_images[0])
       user_img_url = url_for('uploaded_file', filename=final_image_filename)
       print(f"사용자 이미지 발견: {user_img_url}")
   else:
       # 사용자 이미지가 없으면 기본 이미지 사용 (static 폴더)
       user_img_url = url_for('static', filename=f'images/{icon_fixed}.png')
       print(f"기본 이미지 사용: {user_img_url}")

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
      'user_weather_image': user_img_url, # 최종 이미지 URL 사용
      'forecast': hourly_forecast,
      'main_weather_english': main_weather_english # 디버깅용으로 추가
   }

   weather_cache[cache_key] = (time.time(), response_json)
   return jsonify(response_json)

@app.route('/upload')
def upload_page():
   return render_template('upload.html')

@app.route('/upload-image', methods=['POST'])
def upload_image():
    weather_condition = request.form.get('weather')
    if not weather_condition:
        return jsonify({'success': False, 'message': '날씨 조건이 필요합니다.'}), 400

    # 한글 날씨 조건을 영어로 변환 (저장 폴더명/파일명에 사용)
    weather_condition_eng = KOR_TO_ENG_WEATHER.get(weather_condition, weather_condition).lower()

    image_file = request.files.get('image')
    image_url = request.form.get('image_url')

    if image_file:
        original_filename = secure_filename(image_file.filename)
        # 파일명에서 확장자 추출
        file_extension = os.path.splitext(original_filename)[1]
        if file_extension.lower() not in ALLOWED_EXTENSIONS:
            return jsonify({'success': False, 'message': '허용되지 않는 파일 확장자입니다.'}), 400
        
        image_data = image_file.read()

        # save_optimized_image 함수가 저장된 파일명을 반환하도록 수정했으므로, 그 값을 받아서 사용합니다.
        saved_filename = save_optimized_image(image_data, weather_condition_eng, file_extension)
        if not saved_filename:
            return jsonify({'success': False, 'message': '이미지 저장에 실패했습니다.'}), 500
        
        # 클라이언트에게는 저장된 파일명을 반환하여 미리보기 URL을 구성하도록 합니다.
        return jsonify({'success': True, 'message': '이미지 업로드 및 저장 성공!', 'filename': saved_filename}), 200

    elif image_url:
        # URL의 유효성 검사 및 허용된 확장자 확인
        if not re.match(r'https?://.*\.(png|jpg|jpeg|gif|svg|webp)(?:\?|$)', image_url, re.IGNORECASE):
            return jsonify({'success': False, 'message': '유효하지 않은 이미지 URL입니다. (png, jpg, jpeg, gif, svg, webp 확장자만 허용)'}), 400
        
        try:
            response = requests.get(image_url, stream=True, timeout=10)
            response.raise_for_status()
            image_data = response.content
            
            # URL에서 확장자를 추출합니다. (쿼리 파라미터 제거 후)
            file_extension = os.path.splitext(image_url.split('?')[0])[1]
            if not file_extension: # 확장자가 없는 경우 .png 기본값
                file_extension = '.png'

            saved_filename = save_optimized_image(image_data, weather_condition_eng, file_extension)
            if not saved_filename:
                return jsonify({'success': False, 'message': '이미지 저장에 실패했습니다.'}), 500

            return jsonify({'success': True, 'message': '이미지 URL 업로드 및 저장 성공!', 'filename': saved_filename}), 200

        except requests.exceptions.RequestException as e:
            return jsonify({'success': False, 'message': f'이미지 다운로드 실패: {str(e)}'}), 500
    else:
        return jsonify({'success': False, 'message': '이미지 파일 또는 URL을 제공해야 합니다.'}), 400


@app.route('/reset-all-images', methods=['POST'])
def reset_all_images():
   try:
       # 사용자 이미지 폴더 내의 모든 파일 삭제
       for f in glob.glob(os.path.join(USER_IMAGES_FOLDER, '*')):
           if os.path.isfile(f):
               os.remove(f)
       return jsonify({'success': True, 'message': '모든 사용자 이미지가 성공적으로 초기화되었습니다.'}), 200
   except Exception as e:
       return jsonify({'success': False, 'message': f'이미지 초기화 중 오류 발생: {str(e)}'}), 500

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