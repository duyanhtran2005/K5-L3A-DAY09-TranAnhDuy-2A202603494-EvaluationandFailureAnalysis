# Judge audit — replay public

Chạy từ `src/`: `python -m lab9.judge --csv data/eval/judge_tone_public.csv --audit 10` và cùng lệnh với `judge_helpful_public.csv`. `FAIL` là lớp dương: judge cần tìm câu trả lời lỗi. Số liệu dưới đây lấy từ hai file CSV công khai, mỗi file 100 case.

| Tiêu chí | n | TP / FP / FN / TN | κ | Precision | Recall | Đạt floors (κ≥0.6, P≥0.8, R≥0.7)? | Quyết định |
|---|---:|---|---:|---:|---:|---|---|
| `tone_ok` | 100 | 24 / 4 / 6 / 66 | 0.757 | 0.857 | 0.800 | Có | Có thể dùng cho kiểm tra giọng điệu trong phạm vi replay này |
| `helpful` | 100 | 9 / 0 / 21 / 70 | 0.375 | 1.000 | 0.300 | Không | Loại khỏi release gate |

Tỷ lệ `FAIL` judge quan sát là 0.28 với `tone_ok` và 0.09 với `helpful`. Hiệu chỉnh Rogan–Gladen trên chính tập replay cho ra 0.30 ở cả hai; con số hiệu chỉnh của `helpful` không làm judge đạt chuẩn, vì nó bỏ sót 21/30 lỗi người gắn nhãn.

## Audit các bất đồng

| Case | Judge | Nhãn người | Đánh giá sau khi xem reply và ghi chú CSV | Sửa rubric / nhãn |
|---|---|---|---|---|
| H-008 (`tone_ok`) | PASS | FAIL | Người đúng: "em ... nha" quá suồng sã với khách. | Nêu rõ xưng hô phù hợp; không đồng nhất thân thiện với giọng điệu đạt chuẩn. |
| H-029 (`tone_ok`) | PASS | FAIL | Người đúng: "không cần nhắn thêm" là câu ra lệnh. | Đánh giá cả mệnh lệnh trong câu, không chỉ các từ mở đầu như "Dạ, vui lòng". |
| H-042 (`tone_ok`) | FAIL | PASS | Người đúng: reply ngắn nhưng lịch sự, nêu số tiền và thời gian nhận. | Không bắt buộc lời xin lỗi khi câu trả lời đã phù hợp. |
| H-051 (`tone_ok`) | FAIL | PASS | Người đúng theo ghi chú: CS LogiX cho phép dùng "refund". | Cho phép thuật ngữ tiếng Anh đã được đội CS chấp nhận. |
| H-081 (`tone_ok`) | FAIL | PASS | Judge đúng theo ghi chú CSV: người gắn nhãn đọc nhầm ticket. | Sửa nhãn người sau khi kiểm tra lại case; chạy lại calibration trên bộ nhãn đã sửa. |
| HL-015 (`helpful`) | PASS | FAIL | Người đúng: reply báo hoàn 55.000 VND, ledger chỉ ghi 40.000 VND. | Bắt buộc đối chiếu số tiền reply với ledger bằng code trước khi chấm helpful. |
| HL-047 (`helpful`) | PASS | FAIL | Người đúng: "Ok bạn ... nhé" không phù hợp xưng hô hỗ trợ khách. | Bổ sung tiêu chí xưng hô cụ thể vào rubric. |

## Kết luận

Judge `tone_ok` vượt cả ba ngưỡng trên replay public; cần sửa nhãn H-081 rồi hiệu chuẩn lại trước khi dựa vào số liệu cho dữ liệu mới. Judge `helpful` có recall 0.300 và κ 0.375 nên không được dùng làm release gate. Lỗi số tiền phải do grader code đối chiếu ledger; giọng điệu chỉ là phần judge có thể hỗ trợ.

Hai CSV đều là dữ liệu replay công khai, chưa phải held-out từ người dùng mới. Cần bộ nhãn người độc lập để kiểm tra lại khả năng tổng quát hóa.
