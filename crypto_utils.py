from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import base64

KEY = b"TYT!Cc6JpkGgeCtC2YNypA3AgwMX@tyt"

def decrypt_content(base64_input):
    """
    Decrypts a Base64 encoded, AES-CBC encrypted string where the IV is prepended to the ciphertext.
    """
    if not base64_input:
        return ""
    try:
        # Ensure the input is a bytes-like object for b64decode
        if isinstance(base64_input, str):
            base64_input_bytes = base64_input.encode('utf-8')
        else:
            base64_input_bytes = base64_input
            
        # Decode the base64 string
        decoded_data = base64.b64decode(base64_input_bytes)

        # The first 16 bytes are the IV, the rest is the ciphertext
        iv = decoded_data[:16]
        ciphertext = decoded_data[16:]

        # Create AES cipher object
        cipher = AES.new(KEY, AES.MODE_CBC, iv)

        # Decrypt and unpad
        decrypted_padded = cipher.decrypt(ciphertext)
        decrypted = unpad(decrypted_padded, AES.block_size)

        # Decode to UTF-8 string
        return decrypted.decode('utf-8')
    except (ValueError, KeyError, base64.binascii.Error) as e:
        print(f"Decryption failed: {e}. Returning raw input.")
        # If decryption fails, it might be plain text already.
        # It's safer to return the original string if it was a string, not bytes
        if isinstance(base64_input, str):
            return base64_input
        # If the original was bytes, we can't be sure of the encoding, but utf-8 is a good guess
        try:
            return base64_input.decode('utf-8')
        except UnicodeDecodeError:
            return str(base64_input)
    except Exception as e:
        print(f"An unexpected decryption error occurred: {e}")
        if isinstance(base64_input, str):
            return base64_input
        return str(base64_input) 