// tb_latency.v -- testbench around PyRPL's red_pitaya_dsp (the real routing, summing and modules of the
// bitstream, minus the top-level ADC/DAC I/O registers, which run_sim.py adds by hand).
//
// Configured by tb_config.vh, which run_sim.py writes for each experiment:
//   register writes (wr(addr, data);) and these defines:
//   MODE_STEP   : step the ADC input by STEP counts, report cycles until OUT1 moves, dump the response
//   MODE_LOCKIN : ADC input = A(t) sin(w t + PH) + B cos(2 w t), A steps from 0 to AMP at T_STEP_US;
//                 OUT1 is printed every DECIM cycles until T_END_US
`timescale 1ns / 1ps
module tb_latency;
    reg clk = 1'b0;
    always #4 clk = ~clk;                          // 125 MHz, 8 ns period
    reg rstn = 1'b0;
    reg  signed [13:0] adc_a = 14'sd0;
    wire signed [13:0] dac_a, dac_b;
    reg  [31:0] sys_addr = 32'd0, sys_wdata = 32'd0;
    reg  sys_wen = 1'b0, sys_ren = 1'b0;
    wire [31:0] sys_rdata;
    wire sys_err, sys_ack, trig;
    wire [13:0] scope1, scope2, pwm0, pwm1, pwm2, pwm3;

    red_pitaya_dsp #(.MODULES(8)) dut (
        .clk_i(clk), .rstn_i(rstn),
        .dat_a_i(adc_a), .dat_b_i(14'd0), .dat_a_o(dac_a), .dat_b_o(dac_b),
        .scope1_o(scope1), .scope2_o(scope2),
        .asg1_i(14'd0), .asg2_i(14'd0), .asg1phase_i(14'd0),
        .pwm0(pwm0), .pwm1(pwm1), .pwm2(pwm2), .pwm3(pwm3), .trig_o(trig),
        .sys_addr(sys_addr), .sys_wdata(sys_wdata), .sys_sel(4'hF),
        .sys_wen(sys_wen), .sys_ren(sys_ren), .sys_rdata(sys_rdata), .sys_err(sys_err), .sys_ack(sys_ack));

    task wr(input [31:0] a, input [31:0] d);
        begin
            @(negedge clk); sys_addr = a; sys_wdata = d; sys_wen = 1'b1;
            @(negedge clk); sys_wen = 1'b0;
        end
    endtask

    integer k, i;
    reg signed [13:0] base;
    real t_ns, amp, x, w;
    integer cyc;

    initial begin
        repeat (10) @(posedge clk);
        rstn = 1'b1;
        repeat (10) @(posedge clk);
        `include "tb_config.vh"
`ifdef DUMP
        $dumpfile(`DUMP);                           // waveforms for GTKWave (step experiments only)
        $dumpvars(0, tb_latency);
`endif
`ifdef MODE_STEP
        repeat (`SETTLE) @(posedge clk);
        @(negedge clk);
        base = dac_a;
        adc_a = `STEP;                              // seen by the first rising edge after this
        k = 0;
        while (dac_a == base && k < `MAXWAIT) begin
            @(posedge clk); k = k + 1; @(negedge clk);
        end
        $display("LATENCY_CYCLES %0d", k);
        for (i = 0; i < 24; i = i + 1) begin
            $display("STEPRESP %0d %0d", k + i, $signed(dac_a));
            @(negedge clk);
        end
`endif
`ifdef MODE_LOCKIN
        w = 2.0 * 3.141592653589793 * `F_HZ * 1.0e-9;    // rad per ns
        cyc = 0;
        while (cyc * 8.0 < `T_END_US * 1000.0) begin
            @(negedge clk);
            t_ns = cyc * 8.0;
            amp = (t_ns >= `T_STEP_US * 1000.0) ? `AMP : 0.0;
            x = amp * $sin(w * t_ns + `PH) + `B2F * $cos(2.0 * w * t_ns);
            adc_a = $rtoi(x);
            if (cyc % `DECIM == 0)
                $display("S %0.3f %0d", t_ns / 1000.0, $signed(dac_a));
            cyc = cyc + 1;
        end
`endif
        $finish;
    end
endmodule
