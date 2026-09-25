const stripe = require("stripe")(process.env.STRIPE_SECRET_KEY);

async function createCustomer(email) {
  return stripe.customers.create({ email });
}

module.exports = { createCustomer };
