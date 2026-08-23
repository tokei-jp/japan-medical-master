# 営業人員配置 移動負荷係数モデル（長野県パイロット）

二次医療圏単位で可住地面積を基礎とした移動負荷係数（geo_coefficient）を算出し、
必要人員数（FTE）を補正するモデルの長野県パイロット実装。

## セットアップ

```bash
pip install -r requirements.txt
```

## 実行手順

1. ソースファイルを取得する（e-Stat への実アクセスが必要）:

   ```bash
   python scripts/fetch_sources.py
   ```

   ネットワークの都合でスクリプトから取得できない場合は、下記2ファイルを
   手動でダウンロードし `data/raw/` に配置する:

   - `habitable_area.xlsx` — 可住地面積（統計でみる市区町村のすがた 2026、表B自然環境）
     https://www.e-stat.go.jp/stat-search/file-download?statInfId=000040463585&fileKind=0
   - `medical_zone_mapping.xlsx` — 二次医療圏－市区町村対応表（厚労省 病院報告 R5）
     https://www.e-stat.go.jp/stat-search/file-download?statInfId=000040224319&fileKind=4

2. 突合・集計・係数算出を実行する:

   ```bash
   python scripts/build_geo_coefficient.py
   ```

   出力: `data/output/nagano_secondary_medical_zone_geo_coefficient.csv`

## 前提・仮定事項

`docs/ASSUMPTIONS.md` を参照。特に、このプロジェクトを構築したセッションでは
e-Stat への外部ネットワークアクセスがブロックされており、実ファイルの中身を
確認した上での動作検証はできていない点に注意。

## geo_coefficient の計算式

```
spacing_i        = habitable_area_km2_i / target_facility_count_i
benchmark        = median(spacing_i)   # 長野県内二次医療圏の中央値
relative_distance = sqrt(spacing_i / benchmark)
geo_coefficient   = clip(1 + 0.3 * (relative_distance - 1), min=0.85, max=1.30)
adjusted_required_fte = simple_required_fte * geo_coefficient
```

## ネクストステップ

長野県の結果が妥当と確認できたら、同じロジックを全国345二次医療圏に展開する。
