from flask import Flask

app = Flask(__name__)


@app.get("/")
def index():
    return "hi"


@app.route("/about", methods=["GET"])
def about():
    return "about"


@app.post("/submit")
def submit():
    return "ok"
