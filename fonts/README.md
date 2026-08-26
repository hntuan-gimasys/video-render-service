# Fonts

Đặt các file `.ttf` / `.otf` bạn muốn dùng cho phụ đề vào thư mục này.

Yêu cầu quan trọng: font **phải hỗ trợ tiếng Việt có dấu** (Latin Extended
Additional). Các font an toàn, license mở:

- Roboto / Roboto-Bold  — https://fonts.google.com/specimen/Roboto
- Be Vietnam Pro        — https://fonts.google.com/specimen/Be+Vietnam+Pro
- Inter                 — https://fonts.google.com/specimen/Inter
- Montserrat            — https://fonts.google.com/specimen/Montserrat

Giá trị `subtitle.font_name` trong options phải khớp **tên family bên trong file
font**, không phải tên file. Kiểm tra bằng:

```bash
fc-scan --format "%{family}\n" fonts/Roboto-Regular.ttf
```

Image đã cài sẵn DejaVu Sans và Liberation Sans làm fallback.
