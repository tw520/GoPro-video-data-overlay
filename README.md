# GoPro 影片資料疊加

將 GoPro 相機 MP4 中的 **GPS 軌跡、速度、高度** 等 GPMF 遙測資料，疊加到輸出影片上。功能類似 GoPro Quik，並可自訂顯示位置、項目與輸出畫質。

## 功能

- 解析 GoPro GPMF 遙測（GPS5/GPS9 等）
- 疊加 **速度儀表**、**高度**、**GPS 軌跡小地圖**
- 儀表板與 GPS 地圖可各自選擇 **四個角落** 位置
- **疊加大小** 滑桿可調整儀表板與地圖整體比例
- 匯出時以 **百分比** 顯示處理進度，並可 **終止處理** 後重新調整參數
- GPS 地圖優先使用 OpenStreetMap 瓦片；若連線或 SSL 失敗，會自動改用 **簡化 GPS 軌跡** 並繼續匯出
- **匯出 / 匯入 GPS 資料**（JSON、GPX）；匯入後以使用者 GPS 進行疊加
- 輸出畫質：原始 / 4K / 1080p / 720p
- 圖形介面（Gradio）與命令列（CLI）

## 系統需求

- Windows 10/11（亦可在 macOS / Linux 使用）
- Python 3.10+
- 建議安裝 [FFmpeg](https://www.gyan.dev/ffmpeg/builds/) 並加入 PATH（輸出編碼與音訊保留）

## 安裝

```powershell
git clone https://github.com/tw520/GoPro-video-data-overlay.git
cd GoPro-video-data-overlay
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> **Windows 提示：** 若尚未安裝 Python，請至 [python.org](https://www.python.org/downloads/) 下載 3.10 以上版本，安裝時勾選 **Add python.exe to PATH**。

## Windows 快速啟動（推薦）

完成上述安裝後，之後每次使用只需**雙擊專案根目錄的 `run.bat`**，即可啟動圖形介面；腳本會自動切換到專案目錄、啟用虛擬環境並執行 `app.py`。

```
GoPro-video-data-overlay/
└── run.bat    ← 雙擊此檔啟動
```

若雙擊後視窗立刻關閉或出現錯誤，請確認：

1. 已完成「安裝」章節的步驟（專案內須有 `venv` 資料夾）
2. 已安裝 Python 3.10+ 且可在命令提示字元執行 `python --version`
3. 建議已安裝 FFmpeg 並加入 PATH（輸出品質較佳）

也可在命令提示字元或 PowerShell 中手動執行：

```bat
run.bat
```

## 圖形介面

```powershell
python app.py
```

（Windows 使用者亦可直接雙擊 `run.bat`，效果相同。）

瀏覽器會開啟本機介面：

1. 上傳 GoPro **原始 MP4**（非 LRV 低解析度預覽檔）
2. 點「分析遙測資料」確認有 GPS
3. 勾選要顯示的項目、調整角落位置、疊加大小與畫質
4. （可選）「匯出 GPS 資料」保存軌跡，或「匯入 GPS 資料」使用外部 GPS
5. 「更新預覽」→「匯出影片」（處理中可點「終止處理」）

## 命令列

```powershell
python -m gopro_overlay.cli GX010001.MP4 -o output.mp4 ^
  --dashboard-corner bottom_left ^
  --gps-corner bottom_right ^
  --quality 1080p
```

### 常用參數

| 參數 | 說明 |
|------|------|
| `--dashboard-corner` | 儀表板：`top_left` / `top_right` / `bottom_left` / `bottom_right` |
| `--gps-corner` | GPS 地圖位置（同上） |
| `--quality` | `original` / `4k` / `1080p` / `720p` |
| `--no-speed` | 不顯示速度 |
| `--no-gps-track` | 不顯示 GPS 軌跡 |
| `--distance` | 顯示累積距離 |
| `--overlay-scale` | 疊加面板整體大小（預設 1.0） |
| `--import-gps` | 匯入外部 GPS 檔（JSON / GPX） |
| `--gps-time-offset` | GPS 時間偏移（秒） |
| `--export-gps` | 僅匯出 GPS 到指定 `.json` 或 `.gpx` 路徑 |

## 關於 GPS

- 請使用相機內 **原始 MP4**，並確認設定中已開啟 GPS
- 若只有 LRV 檔或經 Quik 重新編碼且遙測被移除，可能無法讀取資料

## 專案結構

```
GoPro-video-data-overlay/
├── app.py                 # Gradio 圖形介面
├── run.bat                # Windows 一鍵啟動（雙擊執行）
├── gopro_overlay/
│   ├── config.py          # 使用者設定
│   ├── extract.py         # GPMF 解析（telemetrik）
│   ├── gps_io.py          # GPS 匯出 / 匯入（JSON、GPX）
│   ├── map_tiles.py       # OpenStreetMap 地圖瓦片
│   ├── render.py          # 疊加繪製
│   ├── process.py         # 影片處理管線
│   ├── video_io.py        # 影片 I/O 與工作目錄
│   └── cli.py             # 命令列
├── requirements.txt
├── LICENSE
└── README.md
```

## 授權

本專案以 [GNU General Public License v3.0](LICENSE)（GPLv3）授權發佈。
