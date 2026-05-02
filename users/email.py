from django.core.mail import get_connection,EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings
from django.utils.html import strip_tags


def send_html_email(
    subject,
    template_name,
    context,
    recipient_list
):
    """
    Send HTML email using Django templates
    """

     # ✅ Open a fresh connection inside the thread
    connection = get_connection()
    connection.open()

    # Render HTML template
    html_content = render_to_string(template_name, context)

    # Create plain text version
    text_content = strip_tags(html_content)

    # Create email
    email = EmailMultiAlternatives(
        subject=subject,
        body=text_content,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipient_list,
        connection=connection   # ← pass connection explicitly
    )

    # Attach HTML version
    email.attach_alternative(html_content, "text/html")

    # Send
    email.send()
    connection.close()   # ← close after sending