# Trace case: C-002@11 tin ticket hơn DB

Chạy trong `src/`:

```bash
python -m lab9.trace_case --shape seeded_regression --claim C-002 --seed 11
python -m lab9.trace_case --shape fixed --claim C-002 --seed 11
python -m pytest -q -m student -o addopts= tests/test_lab9_trace_case.py
```

| Trường | Kết quả |
|---|---|
| Trial | `seeded_regression C-002@11`; report `E-seeded_regression-20261117-4751`, eval set SHA `47513251ee9e` |
| Triệu chứng | Lab 8 grader FAIL: `status resolved != denied`. Reply báo đã hoàn 20.000 VND, ledger cũng ghi 20.000 VND; nhãn yêu cầu `denied`, ledger 0 VND. |
| Bước sai đầu tiên | Event 3 `model_override` với payload `{"delay_days": 5}`. |
| Vì sao là bước đầu tiên | Event 1 `handoff` chỉ giao việc kiểm policy; event 2 `investigator` đọc đơn O-202 trong DB, nơi `delay_days=2`. Ticket nói 5 ngày. Event 3 mới thay dữ kiện DB bằng lời ticket. |
| Failure mode | FM-03 `ticket_trust` trong `docs/failure_taxonomy.md`. |
| Đối chứng | `fixed C-002@11` không có `model_override`; event 3 quote 0 VND (`no_entitlement`), status `denied`, ledger 0 VND, Lab 8 grader PASS. |
| Regression test | `test_ticket_delay_cannot_override_order_record` trong `src/tests/test_lab9_trace_case.py` kiểm event sai, ledger của biến thể lỗi và kết quả đúng của `fixed`. |
| Chưa chắc chắn | Trace của một claim ở một seed chưa cho biết tỷ lệ lỗi trên toàn eval set hoặc độ ổn định với model thật; các số đó cần report nhiều item và seed. |
