"""Server-side QR SVG rendering and EMVCo / Napas247 VietQR dynamic payload builder."""

from __future__ import annotations

import base64
import io

import qrcode
import qrcode.image.svg


def generate_qr_svg_data_uri(payload: str, box_size: int = 10, border: int = 3) -> str:
    """Render a crisp, high-contrast vector SVG QR code as a data URI."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def _crc16_ccitt(data: str) -> str:
    """Compute ISO/IEC 13239 CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) required by EMVCo QR."""
    crc = 0xFFFF
    for byte in data.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def build_vietqr_emv_payload(bank_bin: str, account_number: str, amount: int, transfer_content: str) -> str:
    """Build standard EMVCo / Napas247 dynamic bank transfer QR payload."""
    clean_bin = "".join(ch for ch in (bank_bin or "970436") if ch.isdigit()) or "970436"
    clean_acc = "".join(ch for ch in account_number if ch.isalnum())
    beneficiary = _tlv("00", clean_bin) + _tlv("01", clean_acc)
    merchant_info = _tlv("00", "A000000727") + _tlv("01", beneficiary) + _tlv("02", "QRIBFTTA")
    additional = _tlv("08", transfer_content[:50])

    body = (
        _tlv("00", "01")
        + _tlv("01", "12")
        + _tlv("38", merchant_info)
        + _tlv("53", "704")
        + _tlv("54", str(int(amount)))
        + _tlv("58", "VN")
        + _tlv("62", additional)
        + "6304"
    )
    return body + _crc16_ccitt(body)


def build_topup_qr_bundle(bank_settings: dict, amount: int, transfer_content: str) -> dict:
    """Return EMVCo QR string, inline SVG QR Data URI, and optional custom bank QR URL."""
    emv_payload = build_vietqr_emv_payload(
        bank_bin=bank_settings.get("bank_bin") or "970436",
        account_number=bank_settings["account_number"],
        amount=amount,
        transfer_content=transfer_content,
    )
    return {
        "emv_payload": emv_payload,
        "qr_svg_data_uri": generate_qr_svg_data_uri(emv_payload, box_size=8, border=3),
        "custom_bank_qr_url": bank_settings.get("bank_qr_url") or "",
    }
