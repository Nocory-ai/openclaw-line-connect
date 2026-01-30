"""
Terminal QR Code Generator
"""

import sys


def print_qr_code(url: str, invert: bool = True):
    """
    Display QR Code in terminal
    
    Args:
        url: URL to encode
        invert: Invert colors (for dark terminals)
    """
    try:
        import qrcode
        
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=1,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        
        # Draw using ASCII characters
        qr.print_ascii(invert=invert)
        
    except ImportError:
        # Fallback: prompt user to install or use alternative
        print("╔════════════════════════════════════════════╗")
        print("║  Please install qrcode to display QR Code: ║")
        print("║  pip install qrcode                        ║")
        print("╚════════════════════════════════════════════╝")
        print()
        print("Or open this link directly:")
        print(url)


def generate_qr_image(url: str, output_path: str) -> bool:
    """
    Generate QR Code image file
    
    Args:
        url: URL to encode
        output_path: Output file path
    
    Returns:
        Success status
    """
    try:
        import qrcode
        from qrcode.image.styledpil import StyledPilImage
        from qrcode.image.styles.moduledrawers import RoundedModuleDrawer
        
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=10,
            border=4,
        )
        qr.add_data(url)
        qr.make(fit=True)
        
        # Create QR Code with rounded corners
        img = qr.make_image(
            image_factory=StyledPilImage,
            module_drawer=RoundedModuleDrawer()
        )
        
        img.save(output_path)
        return True
        
    except ImportError:
        # If Pillow not available, use basic version
        try:
            import qrcode
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4,
            )
            qr.add_data(url)
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="black", back_color="white")
            img.save(output_path)
            return True
            
        except Exception as e:
            print(f"Cannot generate QR Code image: {e}")
            return False
    except Exception as e:
        print(f"Cannot generate QR Code image: {e}")
        return False


# Test
if __name__ == '__main__':
    test_url = "https://line.me/R/oaMessage/@572jplep/?bind_test123"
    print("Testing QR Code generation:")
    print()
    print_qr_code(test_url)
    print()
    print(f"URL: {test_url}")
