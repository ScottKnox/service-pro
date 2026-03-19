from PIL import Image, ImageDraw, ImageFont
import os

def create_mock_logo():
    """
    Create a mock company logo image for Steady Work.
    This logo will be used in invoices.
    """
    # Create image with a professional size and background
    width, height = 400, 150
    background_color = (27, 38, 59)  # Dark blue matching brand
    image = Image.new("RGB", (width, height), background_color)
    draw = ImageDraw.Draw(image)
    
    # Add a simple geometric shape (circle/badge)
    circle_color = (243, 249, 250)  # Light color
    margin = 20
    circle_size = height - (2 * margin)
    draw.ellipse(
        [margin, margin, margin + circle_size, margin + circle_size],
        fill=circle_color,
        outline=circle_color
    )
    
    # Add text "STEADY WORK"
    # We'll use default font since we don't have specific fonts available
    text = "STEADY WORK"
    text_color = background_color
    
    # Manual text positioning (center on circle)
    # Approximate text position
    text_x = margin + circle_size + 30
    text_y = (height - 40) // 2
    
    try:
        # Try to use a larger font if available
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 36)
    except (OSError, IOError):
        # Fallback to default font
        font = ImageFont.load_default()
    
    draw.text((text_x, text_y), text, fill=text_color, font=font)
    
    # Add a tagline underneath
    tagline = "Professional HVAC Services"
    try:
        small_font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
    except (OSError, IOError):
        small_font = ImageFont.load_default()
    
    draw.text((text_x, text_y + 45), tagline, fill=text_color, font=small_font)
    
    # Save the logo
    logo_path = os.path.join(os.path.dirname(__file__), "logos", "company_logo.png")
    image.save(logo_path)
    
    print(f"Mock logo created at: {logo_path}")
    return logo_path

if __name__ == "__main__":
    create_mock_logo()
