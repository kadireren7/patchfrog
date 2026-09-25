const stripe = require("stripe")(process.env.STRIPE_SECRET_KEY);

async function createCheckout(successUrl) {
  const session = await stripe.checkout.sessions.create({
    mode: "payment",
    success_url: successUrl,
    payment_method_types: ["card"],
  });
  return session.url;
}

module.exports = { createCheckout };
