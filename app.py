from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_mysqldb import MySQL
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# =========[ CONFIG DATABASE ]=========
app.config["MYSQL_HOST"] = "localhost"
app.config["MYSQL_USER"] = "root"
app.config["MYSQL_PASSWORD"] = "-"
app.config["MYSQL_DB"] = "flask_auth"
app.config["MYSQL_CURSORCLASS"] = "DictCursor"

app.secret_key = "super_secret_key_change_me"

mysql = MySQL(app)


# Helper: ดึงสินค้าหน้า dashboard

def fetch_all_products():
    cur = mysql.connection.cursor()
    cur.execute(
        """
        SELECT id, name, description, price, old_price, image_url, badge, stock
        FROM products
        ORDER BY id ASC
    """
    )
    products = cur.fetchall()
    cur.close()

    # ทำ type ให้แน่นอน
    for p in products:
        p["stock"] = int(p["stock"]) if p["stock"] is not None else 0
        p["price"] = float(p["price"])
        if p["old_price"] is not None:
            p["old_price"] = float(p["old_price"])
    return products


# Helper: ดึง product เดี่ยว

def fetch_product_by_id(pid):
    cur = mysql.connection.cursor()
    cur.execute(
        """
        SELECT id, name, price, image_url, stock
        FROM products
        WHERE id = %s
        LIMIT 1
    """,
        (pid,),
    )
    product = cur.fetchone()
    cur.close()
    if product:
        product["stock"] = int(product["stock"]) if product["stock"] is not None else 0
        product["price"] = float(product["price"])
    return product

# Helper: เพิ่มหรือเพิ่มจำนวน product ใน cart_items

def add_item_to_cart(user_id, product_id):
    cur = mysql.connection.cursor()
    cur.execute(
        """
        SELECT id, qty
        FROM cart_items
        WHERE user_id = %s AND product_id = %s
        LIMIT 1
    """,
        (user_id, product_id),
    )
    row = cur.fetchone()

    if row:
        new_qty = row["qty"] + 1
        cur.execute(
            """
            UPDATE cart_items
            SET qty = %s
            WHERE id = %s
        """,
            (new_qty, row["id"]),
        )
    else:
        cur.execute(
            """
            INSERT INTO cart_items (user_id, product_id, qty)
            VALUES (%s, %s, %s)
        """,
            (user_id, product_id, 1),
        )

    mysql.connection.commit()
    cur.close()

# Helper: ดึงรายการตะกร้าของ user + คำนวณรวม

def fetch_cart_for_user(user_id):
    cur = mysql.connection.cursor()
    cur.execute(
        """
        SELECT 
            ci.product_id,
            ci.qty,
            p.name,
            p.price,
            p.image_url
        FROM cart_items ci
        JOIN products p ON ci.product_id = p.id
        WHERE ci.user_id = %s
        ORDER BY ci.id ASC
    """,
        (user_id,),
    )
    rows = cur.fetchall()
    cur.close()

    items = []
    total = 0.0
    for r in rows:
        price = float(r["price"])
        qty = int(r["qty"])
        line_total = price * qty
        total += line_total
        items.append(
            {
                "product_id": r["product_id"],
                "name": r["name"],
                "qty": qty,
                "price": price,
                "line_total": line_total,
                "image_url": r["image_url"],
            }
        )
    return items, total

# Helper: เคลียร์ตะกร้าหลัง checkout

def clear_cart(user_id):
    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM cart_items WHERE user_id = %s", (user_id,))
    mysql.connection.commit()
    cur.close()

# Helper: สร้างออเดอร์จากตะกร้า
#   - คืนค่า order_id ที่สร้าง

def create_order_from_cart(user_id):
    # 1. ดึงรายการตะกร้าและยอดรวม
    items, total = fetch_cart_for_user(user_id)

    if len(items) == 0:
        return None, "ตะกร้าว่าง ไม่สามารถออกออเดอร์ได้"

    # 2. สร้างออเดอร์ใน orders
    cur = mysql.connection.cursor()
    cur.execute(
        """
        INSERT INTO orders (user_id, total)
        VALUES (%s, %s)
    """,
        (user_id, total),
    )
    mysql.connection.commit()

    order_id = cur.lastrowid

    # 3. สร้าง order_items ทีละรายการ
    for it in items:
        cur.execute(
            """
            INSERT INTO order_items (order_id, product_id, name_snapshot, price_snapshot, qty, line_total)
            VALUES (%s, %s, %s, %s, %s, %s)
        """,
            (
                order_id,
                it["product_id"],
                it["name"],
                it["price"],
                it["qty"],
                it["line_total"],
            ),
        )
    mysql.connection.commit()
    cur.close()

    # 4. เคลียร์ตะกร้า
    clear_cart(user_id)

    return order_id, None


@app.route("/cart/increase", methods=["POST"])
def cart_increase():
    if "user_id" not in session:
        flash("กรุณาล็อกอินก่อนจัดการตะกร้า", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    product_id = request.form.get("product_id", "").strip()

    product = fetch_product_by_id(product_id)
    if not product:
        flash("ไม่พบสินค้า", "error")
        return redirect(url_for("cart"))

    stock_left = product["stock"]
    current_qty_in_cart = get_cart_qty(user_id, product_id)

    if current_qty_in_cart >= stock_left:
        # เช่น stock = 3 แต่ qty=3 แล้ว กด +1 = ไม่ได้
        flash(
            f'สต็อกของ "{product["name"]}" เหลือแค่ {stock_left} ชิ้น ไม่สามารถเพิ่มได้มากกว่านี้',
            "error",
        )
        return redirect(url_for("cart"))

    # ถ้ายังไม่เต็มสต็อก -> เพิ่มได้
    cur = mysql.connection.cursor()
    cur.execute(
        """
        UPDATE cart_items
        SET qty = qty + 1
        WHERE user_id = %s AND product_id = %s
    """,
        (user_id, product_id),
    )
    mysql.connection.commit()
    cur.close()

    flash("เพิ่มจำนวนสินค้าแล้ว (+1)", "success")
    return redirect(url_for("cart"))


# ---------- UPDATE QTY: -1 ----------
@app.route("/cart/decrease", methods=["POST"])
def cart_decrease():
    if "user_id" not in session:
        flash("กรุณาล็อกอินก่อนจัดการตะกร้า", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    product_id = request.form.get("product_id", "").strip()

    # ดึง qty ปัจจุบัน
    cur = mysql.connection.cursor()
    cur.execute(
        """
        SELECT qty
        FROM cart_items
        WHERE user_id = %s AND product_id = %s
        LIMIT 1
    """,
        (user_id, product_id),
    )
    row = cur.fetchone()

    if not row:
        cur.close()
        flash("ไม่พบสินค้านี้ในตะกร้า", "error")
        return redirect(url_for("cart"))

    current_qty = int(row["qty"])

    if current_qty <= 1:
        # ถ้าเหลือ 1 แล้วลดอีก = ลบออก
        cur.execute(
            """
            DELETE FROM cart_items
            WHERE user_id = %s AND product_id = %s
        """,
            (user_id, product_id),
        )
        mysql.connection.commit()
        cur.close()

        flash("เอาสินค้าออกจากตะกร้าแล้ว", "info")
        return redirect(url_for("cart"))

    # ถ้ามากกว่า 1 ก็ลดลง 1
    cur.execute(
        """
        UPDATE cart_items
        SET qty = qty - 1
        WHERE user_id = %s AND product_id = %s
    """,
        (user_id, product_id),
    )
    mysql.connection.commit()
    cur.close()

    flash("อัปเดตจำนวนสินค้าแล้ว (-1)", "success")
    return redirect(url_for("cart"))


def get_cart_qty(user_id, product_id):
    cur = mysql.connection.cursor()
    cur.execute(
        """
        SELECT qty
        FROM cart_items
        WHERE user_id = %s AND product_id = %s
        LIMIT 1
    """,
        (user_id, product_id),
    )
    row = cur.fetchone()
    cur.close()
    return int(row["qty"]) if row else 0


# ---------- REMOVE ITEM (ลบทั้งรายการ) ----------
@app.route("/cart/remove", methods=["POST"])
def cart_remove():
    if "user_id" not in session:
        flash("กรุณาล็อกอินก่อนจัดการตะกร้า", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    product_id = request.form.get("product_id", "").strip()

    cur = mysql.connection.cursor()
    cur.execute(
        """
        DELETE FROM cart_items
        WHERE user_id = %s AND product_id = %s
    """,
        (user_id, product_id),
    )
    mysql.connection.commit()
    cur.close()

    flash("เอาสินค้าออกจากตะกร้าแล้ว", "info")
    return redirect(url_for("cart"))


@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


# ---------- REGISTER ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if username == "" or email == "" or password == "":
            flash("กรุณากรอกข้อมูลให้ครบ", "error")
            return redirect(url_for("register"))

        if len(password) < 6:
            flash("รหัสผ่านต้องยาวอย่างน้อย 6 ตัวอักษร", "error")
            return redirect(url_for("register"))

        cur = mysql.connection.cursor()
        cur.execute(
            "SELECT id FROM users WHERE username=%s OR email=%s", (username, email)
        )
        existing = cur.fetchone()
        if existing:
            flash("username หรือ email นี้ถูกใช้แล้ว", "error")
            cur.close()
            return redirect(url_for("register"))

        pw_hash = generate_password_hash(password)

        cur.execute(
            """
            INSERT INTO users (username, email, password_hash)
            VALUES (%s, %s, %s)
        """,
            (username, email, pw_hash),
        )
        mysql.connection.commit()

        new_user_id = cur.lastrowid
        cur.close()

        # auto login
        session["user_id"] = new_user_id
        session["username"] = username
        flash("สมัครสมาชิกสำเร็จ และล็อกอินให้แล้วค่ะ 🍰", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html")


# ---------- LOGIN ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username_or_email = request.form.get("username_or_email", "").strip()
        password = request.form.get("password", "")

        cur = mysql.connection.cursor()
        cur.execute(
            """
            SELECT id, username, email, password_hash
            FROM users
            WHERE username=%s OR email=%s
        """,
            (username_or_email, username_or_email),
        )
        user = cur.fetchone()
        cur.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            flash("ล็อกอินสำเร็จ", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("ข้อมูลล็อกอินไม่ถูกต้อง", "error")
            return redirect(url_for("login"))

    return render_template("login.html")


# ---------- DASHBOARD ----------
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        flash("กรุณาล็อกอินก่อนเข้าหน้านี้", "error")
        return redirect(url_for("login"))

    products = fetch_all_products()
    return render_template(
        "dashboard.html", username=session.get("username"), products=products
    )


# ---------- CART PAGE ----------
@app.route("/cart")
def cart():
    if "user_id" not in session:
        flash("กรุณาล็อกอินก่อนดูตะกร้า", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    items, total = fetch_cart_for_user(user_id)

    return render_template(
        "cart.html", username=session.get("username"), items=items, total=total
    )


# ---------- ADD TO CART ----------
@app.route("/add_to_cart", methods=["POST"])
def add_to_cart():
    if "user_id" not in session:
        flash("กรุณาล็อกอินก่อนใส่ตะกร้า", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]
    product_id = request.form.get("product_id", "").strip()

    product = fetch_product_by_id(product_id)
    if not product:
        flash("สินค้านี้ไม่มีอยู่ในระบบ", "error")
        return redirect(url_for("dashboard"))

    # สต็อกคงเหลือจริง
    stock_left = product["stock"]

    # ของที่ user มีอยู่ในตะกร้าแล้ว
    current_qty_in_cart = get_cart_qty(user_id, product_id)

    # ถ้าในตะกร้าเรามีอยู่แล้วเท่ากับ stock อยู่แล้ว -> ห้ามเพิ่ม
    if current_qty_in_cart >= stock_left:
        if stock_left <= 0:
            flash("สินค้านี้หมดแล้ว 😢", "error")
        else:
            flash(
                f'มี "{product["name"]}" ในตะกร้าครบจำนวนสต็อกแล้ว ({stock_left} ชิ้น)',
                "error",
            )
        return redirect(url_for("dashboard"))

    # ผ่าน -> เพิ่มเข้า cart_items (เดิม)
    add_item_to_cart(user_id, product_id)

    flash(f"เพิ่ม {product['name']} ลงตะกร้าแล้ว 🛒", "success")
    return redirect(url_for("dashboard"))


# ---------- CHECKOUT (สร้างออเดอร์) ----------
@app.route("/checkout", methods=["POST"])
def checkout():
    if "user_id" not in session:
        flash("กรุณาล็อกอินก่อนสั่งซื้อ", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]

    # 1. ดึงตะกร้า
    items, total = fetch_cart_for_user(user_id)

    if len(items) == 0:
        flash("ตะกร้าว่าง ไม่สามารถสั่งซื้อได้", "error")
        return redirect(url_for("cart"))

    # 2. ตรวจสต็อกแบบจริงจังก่อนอนุมัติ
    #    ถ้าเจอรายการไหนเกินสต็อก -> หยุดเลย
    for it in items:
        pid = it["product_id"]
        qty_want = it["qty"]

        product = fetch_product_by_id(pid)
        if not product:
            flash(f"สินค้าบางอย่างไม่มีในระบบแล้ว (product_id {pid})", "error")
            return redirect(url_for("cart"))

        if qty_want > product["stock"]:
            flash(
                f'สินค้า "{product["name"]}" มีสต็อก {product["stock"]} ชิ้น แต่ในตะกร้าคุณสั่ง {qty_want} ชิ้น กรุณาปรับจำนวนก่อน',
                "error",
            )
            return redirect(url_for("cart"))

    # 3. ผ่านการเช็คสต็อกหมดแล้ว -> สร้างออเดอร์
    order_id, err = create_order_from_cart(user_id)

    if err:
        flash(err, "error")
        return redirect(url_for("cart"))

    # 4. ตัด stock ของสินค้าตามที่สั่งซื้อ
    # NOTE: create_order_from_cart() ตอนนี้เคลียร์ตะกร้าแล้ว
    # เราต้องใช้ items ที่เราดึงก่อนสร้างออเดอร์ (ยังอยู่ในตัวแปร items)

    cur = mysql.connection.cursor()
    for it in items:
        pid = it["product_id"]
        qty_want = it["qty"]
        # UPDATE products SET stock = stock - qty_want WHERE id = pid
        cur.execute(
            """
            UPDATE products
            SET stock = stock - %s 
            WHERE id = %s
        """,
            (qty_want, pid),
        )
    mysql.connection.commit()
    cur.close()

    flash(f"สั่งซื้อเรียบร้อย! หมายเลขคำสั่งซื้อ #{order_id}", "success")
    return redirect(url_for("orders"))


# ---------- ORDER HISTORY ----------
@app.route("/orders")
def orders():
    if "user_id" not in session:
        flash("กรุณาล็อกอินก่อนดูออเดอร์", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]

    # ดึงหัวออเดอร์ทั้งหมดของ user
    cur = mysql.connection.cursor()
    cur.execute(
        """
        SELECT id, total, created_at
        FROM orders
        WHERE user_id = %s
        ORDER BY id DESC
    """,
        (user_id,),
    )
    orders_list = cur.fetchall()
    cur.close()

    # แปลง type ให้ชัด
    for o in orders_list:
        o["total"] = float(o["total"])

    return render_template(
        "orders.html", username=session.get("username"), orders=orders_list
    )


# (ออเดอร์เดี่ยวละเอียด)
@app.route("/order/<int:order_id>")
def order_detail(order_id):
    if "user_id" not in session:
        flash("กรุณาล็อกอินก่อนดูออเดอร์", "error")
        return redirect(url_for("login"))

    user_id = session["user_id"]

    # ดึงหัวบิล ต้องเป็นของ user คนนี้เท่านั้น
    cur = mysql.connection.cursor()
    cur.execute(
        """
        SELECT id, total, created_at
        FROM orders
        WHERE id = %s AND user_id = %s
        LIMIT 1
    """,
        (order_id, user_id),
    )
    order_row = cur.fetchone()

    if not order_row:
        cur.close()
        flash("ไม่พบบิลนี้", "error")
        return redirect(url_for("orders"))

    order_row["total"] = float(order_row["total"])

    # ดึงรายการสินค้าในบิลนี้
    cur.execute(
        """
        SELECT name_snapshot, price_snapshot, qty, line_total
        FROM order_items
        WHERE order_id = %s
        ORDER BY id ASC
    """,
        (order_id,),
    )
    items = cur.fetchall()
    cur.close()

    for it in items:
        it["price_snapshot"] = float(it["price_snapshot"])
        it["line_total"] = float(it["line_total"])

    return render_template(
        "order_detail.html",
        username=session.get("username"),
        order=order_row,
        items=items,
    )


# ---------- LOGOUT ----------
@app.route("/logout")
def logout():
    session.clear()
    flash("ออกจากระบบแล้ว", "info")
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(debug=True)
