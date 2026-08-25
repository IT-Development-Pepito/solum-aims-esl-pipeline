# Laporan Troubleshooting Discount Mismatch ESL

## Item `101024011793` — Store `084`

**Tanggal analisis:** 24 Agustus 2026  
**Lingkup:** Arsitektur ESL yang sedang berjalan saat ini (`RefreshESL_New` → `ESL.dbo.tb_ESL` → Apache Hop → AIMS → perangkat ESL)

---

## 1. Ringkasan Eksekutif

Kasus yang dianalisis adalah mismatch harga promosi untuk item `101024011793` di Store `084`.

Ekspektasi bisnis yang disampaikan:

- harga normal: `92.500`;
- tipe promosi: `FIXED PRICE`;
- harga promo: `69.900`.

Kondisi aktual:

- perangkat ESL menampilkan `75.500`;
- `ESL.dbo.tb_ESL` berisi `SALES_PRICE = 75.500`;
- `tb_ESL` tidak memilih promo `FIXED PRICE 69.900`;
- `tb_ESL` justru berisi promo `PERCENT BASED 10%` dari campaign MODIS.

Troubleshooting menemukan dua jalur masalah terpisah:

1. **Pemilihan promosi di `RefreshESL_New` tidak deterministik.** Satu item dapat mempunyai beberapa campaign overlap, tetapi stored procedure belum mempunyai rule prioritas promo yang eksplisit dan filtering eligibility belum memperhitungkan jam promo secara memadai.
2. **Source harga normal di `RefreshESL_New` belum membatasi `BSP_PRICE_CATG`.** Pada Store `084`, category `001` dan `007` dapat aktif bersamaan. Pada sebagian item harganya berbeda, sehingga current query dapat menghasilkan harga tidak konsisten.

Untuk item `101024011793`, category `001` dan `007` sama-sama bernilai `75.500`. Jadi ambiguity price category bukan penyebab nilai `75.500` pada kasus spesifik ini. Nilai `92.500` tidak ditemukan pada current `BASIC_SP_MST`.

---

## 2. Kondisi Awal Kasus

### Ekspektasi

| Field                               |     Ekspektasi |
| ----------------------------------- | -------------: |
| Store                               |          `084` |
| Item                                | `101024011793` |
| Harga normal                        |       `92.500` |
| Promotion Type                      |  `FIXED PRICE` |
| Harga promo                         |       `69.900` |
| Harga di ESL yang seharusnya tampil |       `69.900` |

### Kondisi aktual `tb_ESL`

| Field              | Nilai aktual                                                 |
| ------------------ | ------------------------------------------------------------ |
| `STORE_CODE`       | `084`                                                        |
| `ITEM_CODE`        | `101024011793`                                               |
| `SALES_PRICE`      | `75.500`                                                     |
| `DISC_PRICE`       | `0`                                                          |
| `DISC_PERCENT`     | `10`                                                         |
| `SAVE_AMT`         | `67.950`                                                     |
| `PROMO_FLAG`       | `1`                                                          |
| `PROMOTION_TYPE`   | `PERCENT BASED`                                              |
| `CAMPAIGN_GROUP`   | `PFS`                                                        |
| `DISC_TEXT`        | `MODIS 10% FRUIT,VEGE,MEAT,SEAFOOD AND FROZEN-BALI (UPDATE)` |
| `PROMO_START_TIME` | `06:00:00`                                                   |
| `PROMO_END_TIME`   | `09:00:00`                                                   |

Ada inkonsistensi internal: `DISC_TEXT` berasal dari MODIS, tetapi `CAMPAIGN_GROUP` berisi PFS. Ini mengindikasikan field promo dapat berasal dari source campaign yang berbeda.

---

## 3. Campaign Aktif untuk Item

Ditemukan tiga campaign `RUNNING`:

| Campaign              | Tipe            |    Nilai | Periode            | Jam         | Hari       |
| --------------------- | --------------- | -------: | ------------------ | ----------- | ---------- |
| MODIS                 | `PERCENT BASED` |      10% | 01 Jun–31 Agu 2026 | 06:00–09:00 | Semua hari |
| PFS FRUIT&VEGE MEMBER | `PERCENT BASED` |       5% | 03 Agu–04 Sep 2026 | 09:01–23:59 | Monday     |
| IN STORE PROMO        | `FIXED PRICE`   | `69.900` | 16–31 Agu 2026     | 00:00–23:59 | Semua hari |

Diagnostic query yang meniru current selection menunjukkan ketiganya dianggap eligible.

---

## 4. Temuan Utama — Promotion Selection

### 4.1 Multiple campaign match ke satu target

Current logic secara konseptual:

```text
ITEM + UOM
   |
   +-- MODIS 10%
   +-- PFS 5%
   +-- IN STORE FIXED 69.900
   |
   v
UPDATE satu row #tmpResult
```

Belum ada rule eksplisit yang menentukan campaign pemenang.

Rule yang masih harus dikonfirmasi:

- apakah fixed price selalu menang atas percentage;
- apakah IN STORE PROMO memiliki priority lebih tinggi;
- apakah member/PFS boleh menjadi public ESL promo;
- apakah prioritas menggunakan Campaign Group, Campaign Type, Campaign Code, harga terendah, atau rule lain.

### 4.2 Jam promo belum menjadi eligibility rule yang memadai

Current diagnostic menggunakan periode tanggal, tetapi `CMP_FROM_TIME`/`CMP_TO_TIME` belum menjadi bagian efektif dari selection.

Akibatnya campaign MODIS `06:00–09:00` dapat tetap dianggap eligible setelah pukul `09:00` selama tanggal campaign masih valid.

### 4.3 Day-of-week perlu dimasukkan bila sesuai rule POS

Source menyimpan Sunday–Saturday flags. PFS pada kasus ini hanya aktif Monday.

Perlu konfirmasi bahwa stored procedure harus mengikuti day-of-week flag dan kemudian memasukkannya ke campaign eligibility.

### 4.4 Semua field promo harus berasal dari satu selected campaign

Setelah satu campaign dipilih, field berikut harus berasal dari row yang sama:

- `DISC_TEXT`
- `DISC_PRICE`
- `DISC_PERCENT`
- `PROMO_FLAG`
- `PROMOTION_TYPE`
- `CAMPAIGN_GROUP`
- `PROMO_START_DATE`
- `PROMO_END_DATE`
- `PROMO_START_TIME`
- `PROMO_END_TIME`

---

## 5. Temuan Harga Normal (`BASIC_SP_MST`)

Untuk Store `084`, item `101024011793`, UOM `KGS`:

| Price Category | Sell Price |      MRP | Status |
| -------------- | ---------: | -------: | ------ |
| `001`          |   `75.500` | `75.500` | `A`    |
| `007`          |   `75.500` | `75.500` | `A`    |

Query khusus `BSP_SELL_PRICE = 92500` tidak mengembalikan row.

Kesimpulan:

> `RefreshESL_New` tidak menghasilkan angka `75.500` sendiri. Angka tersebut berasal dari current price master Store `084`.

Nilai `92.500` perlu ditelusuri ke source bisnis lain sebelum dianggap sebagai current normal price yang benar.

---

## 6. Temuan Systemic — `BSP_PRICE_CATG`

Profiling Store `084`:

| Metric                              |   Nilai |
| ----------------------------------- | ------: |
| Item/UOM dengan >1 active price row | `3.826` |
| Category berbeda tetapi harga sama  | `3.737` |
| Category dengan harga berbeda       |    `89` |
| Persentase harga berbeda            | `2,33%` |
| Maximum active rows per Item/UOM    |     `2` |

Distribusi:

| Category | Active Rows | Distinct Items |
| -------- | ----------: | -------------: |
| `001`    |    `17.347` |       `14.195` |
| `007`    |     `3.832` |        `3.123` |

Perbandingan `001` vs `007`:

| Kondisi     |  Jumlah |
| ----------- | ------: |
| Harga sama  | `3.737` |
| `007 > 001` |    `53` |
| `007 < 001` |    `36` |

Perbandingan dengan `tb_ESL`:

| Metric                         |   Nilai |
| ------------------------------ | ------: |
| Compared items                 | `2.895` |
| Match `001` saat harga berbeda |    `48` |
| Match `007` saat harga berbeda |     `8` |
| Category sama harga            | `2.839` |
| Match neither                  |     `1` |

Artinya current `RefreshESL_New` tidak konsisten hanya mengambil satu price category ketika source category berbeda nilai.

---

## 7. Evidence dari Existing POS Logic

### `FN_GET_SELLPRICE`

Memakai:

```sql
BSP_METHOD = 1
BSP_PRICE_CATG = '001'
BSP_STATUS = 'A'
```

Fallback ke HO juga tetap memakai category `001`.

### `RXLSP_GET_SELLPRICE`

Memakai category `001` untuk:

- batch item;
- non-batch item;
- HO fallback.

### `RXLSP_GET_ITEM_PRICE`

Menerima parameter `@PRICE_CATEGORY`, menandakan price category merupakan business dimension yang memang harus dipilih secara eksplisit.

### Working conclusion

`BSP_PRICE_CATG='001'` adalah kandidat paling kuat untuk normal/default selling price ESL, tetapi arti bisnis `001` dan `007` tetap perlu dikonfirmasi sebelum perubahan production.

---

## 8. Temuan Downstream Apache Hop / AIMS

Current page mapping:

| Page | Fungsi                  |
| ---: | ----------------------- |
|    1 | Normal price            |
|    2 | Fixed-price promotion   |
|    3 | Percent-based promotion |
|    4 | Out of stock            |

Fixed-price promo diarahkan ke Page 2 dan REST branch aktif.

Percent-based promo diarahkan ke Page 3, tetapi current hop:

```text
Enhanced JSON Output PAGE 3
→ REST client PG 3
```

berstatus `enabled = N`.

Selain itu Page 3 memakai `${PARAM_STORE_CODE}`, sedangkan flow lain memakai `${STORE_CODE_PARAM}`.

Implikasi:

- salah klasifikasi promo di SQL Server dapat mengarahkan item ke Page 3;
- Page 3 tidak mengirim REST request;
- label dapat tetap pada page sebelumnya.

Ini adalah contributing issue downstream, bukan root cause awal selection campaign.

---

## 9. Root Cause Classification

### Confirmed — Promotion

`RefreshESL_New` belum mempunyai deterministic campaign selection untuk overlapping campaign.

Contributing factors:

1. time eligibility belum lengkap;
2. day-of-week belum menjadi rule selection yang terverifikasi;
3. tidak ada campaign priority;
4. satu Item/UOM dapat match ke beberapa campaign;
5. promo detail dan promo metadata dapat berasal dari source berbeda.

### Confirmed — Source harga current

`BASIC_SP_MST` Store `084` menyimpan `75.500`, bukan `92.500`, untuk item kasus.

### Confirmed — Systemic price-selection defect

`RefreshESL_New` membaca active `BASIC_SP_MST` tanpa menentukan `BSP_PRICE_CATG`, sehingga item dengan `001 != 007` dapat menghasilkan price tidak konsisten.

---

## 10. Hal yang Harus Dikonfirmasi ke Store / POS

### A. Price category

1. Apa arti `001`?
2. Apa arti `007`?
3. Category mana yang resmi untuk normal public shelf price?
4. Apakah category `001` memang standard/default retail price?

### B. Source `92.500`

1. Dari sistem mana angka `92.500` berasal?
2. Apakah itu current POS price, old price, ERP/merchandising price, atau manual reference?
3. Apakah dibandingkan pada UOM yang sama?
4. Kapan harga `92.500` terakhir valid?

### C. Promotion priority

1. `IN STORE FIXED 69.900` vs `PFS 5%` vs `MODIS 10%`: mana harus menang?
2. Apakah fixed price selalu menang?
3. Apakah member promo boleh ditampilkan di public ESL?
4. Apa ranking resmi antar Campaign Group/Type?

### D. Time/day rules

Konfirmasi bahwa eligibility harus mengikuti:

- start/end date;
- `FROM_TIME`/`TO_TIME`;
- Sunday–Saturday flags.

### E. Page 3 percent promo

Konfirmasi apakah Page 3 memang sengaja disabled atau seharusnya aktif.

### F. UOM weighted item

Campaign dan source menggunakan `KGS`, sementara `tb_ESL` terlihat `/100GR`.

Konfirmasi:

- conversion rule;
- basis harga promo;
- expected display per kg/per 100 gram.

---

## 11. Action Plan

### Phase 1 — Business Confirmation

- konfirmasi semantic `001/007`;
- konfirmasi source resmi `92.500`;
- konfirmasi promo priority;
- konfirmasi day/time rules;
- konfirmasi Page 3;
- konfirmasi UOM conversion.

### Phase 2 — Fix `RefreshESL_New`

#### Selling Price

Setelah confirmation, gunakan source deterministic, kandidat:

```sql
WHERE BSP_METHOD = 1
  AND BSP_ORG_CD = @StoreCode
  AND BSP_PRICE_CATG = '001'
  AND BSP_STATUS = 'A'
```

#### Campaign Eligibility

Harus mempertimbangkan:

- Store;
- Item;
- UOM;
- status campaign;
- status item;
- start/end date;
- start/end time;
- day-of-week;
- supported promotion type.

#### Campaign Priority

Gunakan candidate set + deterministic ranking:

```text
STORE + ITEM + UOM
      ↓
eligible campaigns
      ↓
business priority
      ↓
ROW_NUMBER()
      ↓
rn = 1
```

#### Single Update

Semua promo fields diisi dari selected campaign yang sama.

#### Store Isolation

Pastikan temporary campaign data terikat ke `STORE_CODE` atau dibersihkan per store.

---

## 12. Regression Test Wajib

1. Single fixed-price promo.
2. Single percentage promo.
3. Overlap fixed + percentage.
4. Promo yang jamnya sudah expired.
5. Monday-only promo di hari non-Monday.
6. `001 != 007`.
7. Same item pada store berbeda.
8. Weighted/UOM item (`KGS` → `/100GR`).
9. End-to-end `RefreshESL_New` → `tb_ESL` → Hop → AIMS → physical ESL.

---

## 13. Acceptance Criteria untuk `101024011793`

Jika `IN STORE PROMO` dikonfirmasi sebagai pemenang:

- `PROMOTION_TYPE = FIXED PRICE`;
- `DISC_PRICE = 69900`;
- `DISC_PERCENT = 0`;
- `DISC_TEXT` berasal dari campaign `1001800017386`;
- `CAMPAIGN_GROUP` berasal dari selected campaign yang sama;
- Hop mengarahkan label ke Page 2;
- REST Page 2 sukses;
- physical ESL menampilkan `69.900`;
- normal price memakai source/category yang disetujui POS.

---

## 14. Status Pekerjaan

| Area                              | Status                              |
| --------------------------------- | ----------------------------------- |
| Reproduce mismatch di `tb_ESL`    | Selesai                             |
| Identify active campaigns         | Selesai                             |
| Identify overlapping promo issue  | Selesai                             |
| Identify time eligibility issue   | Selesai                             |
| Investigate source `75.500`       | Selesai                             |
| Search `92.500` di `BASIC_SP_MST` | Selesai — tidak ditemukan           |
| Quantify `001/007` ambiguity      | Selesai                             |
| Inspect POS sell-price functions  | Selesai                             |
| Confirm semantic `001/007`        | **Pending Store/POS**               |
| Confirm source `92.500`           | **Pending Store/POS**               |
| Confirm campaign priority         | **Pending Store/POS/Merchandising** |
| Confirm Page 3 behavior           | **Pending**                         |
| Modify `RefreshESL_New`           | Belum dilakukan                     |
| Regression test                   | Belum dilakukan                     |
| Production deployment             | Belum dilakukan                     |

---

## 15. Rekomendasi ke Manajemen

Jangan melakukan manual update permanen langsung ke `tb_ESL` karena stored procedure dapat menimpanya pada execution berikutnya.

Perbaikan disarankan melalui:

1. business-rule confirmation;
2. deterministic selling-price source;
3. deterministic campaign selection;
4. regression test;
5. controlled production deployment.

Item `101024011793` sebaiknya dijadikan **reference regression case**.

---

## 16. Sumber Analisis

- `Refresh_ESL_NEW.sql`
- `DDL_ESL.sql`
- `ESL_Sample_Data.xlsx`
- `troubleshoot_discount_mismatch.sql`
- query `BASIC_SP_MST` Store `084`
- profiling `BSP_PRICE_CATG 001/007`
- definition `FN_GET_SELLPRICE`
- definition `RXLSP_GET_SELLPRICE`
- definition `GET_CUSTOMER_CATEGORY_WISE_SP`
- definition `RXLSP_GET_ITEM_PRICE`
- `SOLUM_ESL_Apache_Hop_Architecture_and_Runbook.md`

---

**Kesimpulan singkat:** root cause utama discount mismatch sudah terlokalisasi pada logic pemilihan promotion di `RefreshESL_New`. Investigasi juga menemukan defect tambahan pada pemilihan normal selling price karena `BSP_PRICE_CATG` belum ditentukan secara eksplisit. Sebelum perubahan production, masih diperlukan konfirmasi business rule dari Store/POS mengenai price category, sumber harga `92.500`, promotion priority, validasi waktu/hari, UOM, dan behavior percent-promo Page 3.
