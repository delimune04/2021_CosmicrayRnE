# 2021 Cosmicray R&E - Forbush Decrease

경기북과학고등학교 2021 R&E의 "우주선 Forbush Decrease 탐색을 위한 기상환경 보정 알고리즘"을 재현 가능하게 실행하는 파이썬 분석 도구입니다. 기존 C++/ROOT 매크로에 흩어져 있던 자료 읽기, 일별 Flux 산출, 선형 회귀 보정, FD 판별을 하나의 명령으로 실행합니다. 외부 패키지와 ROOT 설치가 필요하지 않습니다.

## 포함된 분석 절차

1. COREA 검출 자료를 읽어 일별 event 수(연구에서 사용한 Flux), CH2/CH3 에너지 침적량과 비율을 집계합니다.
2. 기상청·교내 기상장비 자료를 날짜별로 결합하고, 선택한 기상변수의 다중 선형회귀 모델을 적합합니다.
3. 관측기간 평균 기상을 기준값으로 하여 `보정 Flux = 관측 Flux + 예측(평균 기상) - 예측(당일 기상)`을 계산합니다.
4. 보정 Flux에서 다음 2021 R&E 기준을 모두 충족하는 Forbush Decrease 후보를 찾습니다.
   - 직전 7개 연속 관측일의 평균인 기준선 대비 2% 이상 감소
   - 기준일 뒤 1-7일 안에 최저점
   - 최저점 뒤 5-12일 안에 기준선의 96% 이상 회복
   - 최저점부터 회복일까지의 선형 회복 추세가 양의 기울기이고 R² >= 0.9198

날짜가 비어 있는 구간은 검출기 가동 중단을 물리 현상으로 오인하지 않도록 후보에서 제외합니다.

## 빠른 시작

Python 3.10 이상에서 저장소 루트에서 실행합니다. 설치하지 않고도 다음처럼 사용할 수 있습니다.

```powershell
$env:PYTHONPATH = "src"
python -m cosmicray_rne `
  --detector "C:\path\to\RawData" `
  --weather "C:\path\to\RawWeatherData" `
  --output-dir output
```

또는 editable 설치 후 명령어를 사용할 수 있습니다.

```powershell
python -m pip install -e .
cosmicray-rne --detector "C:\path\to\RawData" --weather "C:\path\to\RawWeatherData"
```

기본 회귀변수는 `temperature humidity`입니다. 흑점 수까지 포함하려면 다음처럼 지정합니다.

```powershell
python -m cosmicray_rne --detector RawData --weather RawWeatherData `
  --features temperature humidity sunspots --output-dir output
```

기상 보정을 생략하고 원시 Flux만으로 후보를 확인하려면 `--skip-correction`을 추가합니다. 모든 FD 기준은 `--baseline-days`, `--minimum-drop`, `--min-decline-days`, `--max-decline-days`, `--min-recovery-days`, `--max-recovery-days`, `--recovered-fraction`, `--minimum-recovery-r-squared`로 명시적으로 바꿀 수 있습니다.

## 입력 형식

검출 자료는 파일 하나 또는 하위 폴더 전체를 받을 수 있으며, 두 형식을 자동 판별합니다.

- **처리된 COREA 자료**: 공백 또는 쉼표로 구분한 9열: `id year month day hour minute second ch2 ch3`. 기존 `RawData`의 `2020-6-13_Lv1_temp.txt`처럼 시간과 CH2/CH3 값이 이미 한 줄에 있는 형식입니다.
- **원본 COREA Level-1 자료**: 한 event가 5정수 x 102행으로 구성된 형식입니다. index 1과 2에서 시간을 복원하고 index 7-101의 CH2/CH3 ADC를 합산합니다.

기본 에너지 침적량은 `max(0, 1024 - ADC)`입니다. 과거 Level-1 처리 코드의 pedestal 100을 재현해야 한다면 `--pedestal 100`을 지정합니다.

날씨 자료도 파일 또는 폴더를 받을 수 있습니다.

- 간단한 6열 형식: `year month day temperature humidity sunspots`
- R&E의 16열 일별 형식: `year month day solar_flux sunspots ... temperature humidity`
- 헤더가 있는 CSV/TSV: `date` 또는 `year,month,day`와 `temperature`, `humidity`, `pressure`, `sunspots`, `solar_flux`, `geomagnetic_ap`, `geomagnetic_sum` 중 필요한 열

같은 날짜에 값이 여러 개면 변수별 평균으로 합칩니다. 잘못된 레코드 수는 `summary.json`에 남아 자료 품질을 점검할 수 있습니다.

## 연구 기준과 구현 범위

FD의 감소율 2%, 회복률 96%, 회복선 R² 0.9198과 감소·회복 기간은 2021 R&E 최종 발표자료의 판별 기준을 그대로 기본값으로 사용했습니다. 발표자료에서 기준선 계산 구간은 수치로 정하지 않았기 때문에, 이 구현에서는 사건 시작 전 7개 연속 관측일의 평균을 기준선으로 정의했습니다. `--baseline-days`로 이 가정을 바꿀 수 있습니다.

최종 연구에서는 지수함수형 회복을 판별하는 계수 D를 표본 부족으로 결정하지 못했습니다. 따라서 현재 탐색기는 연구에서 수치가 정해진 선형 회복 조건만 판별합니다. 출력되는 항목은 자동 탐색 후보이며, 실제 FD 확정에는 태양풍·지자기 자료 및 다른 관측소 자료와의 교차 검증이 필요합니다.

2GB가 넘는 기존 관측 폴더를 처리할 수 있도록 event 전체를 메모리에 적재하지 않고 파일을 순차적으로 읽어 날짜별 통계만 유지합니다.

## 출력

`--output-dir`에 다음 파일을 만듭니다.

- `daily_observations.csv`: 일별 Flux, 에너지 침적량, 관측시간 범위, 기상값, 보정 Flux
- `weather_correction_model.json`: 회귀계수, R², 표본 수, 평균 기상 기준값 (기상 보정 시)
- `forbush_candidates.csv`: 조건을 통과한 FD 후보와 감소·회복 지표
- `summary.json`: 입출력 건수와 생성 파일 목록

## 검증

표준 라이브러리 `unittest`만 사용합니다.

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```
