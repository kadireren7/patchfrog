const stripe = require("stripe")(process.env.STRIPE_SECRET_KEY);

// Does not pass payment_method_types: the rename cannot affect this call.
async function createSubscriptionCheckout(successUrl) {
  return stripe.checkout.sessions.create({ mode: "subscription", success_url: successUrl });
}

module.exports = { createSubscriptionCheckout };
