# build.tcl - recreate the Vivado project of one design from its block design and build it.
#
#   vivado -mode batch -source vivado/build.tcl -tclargs p1|p4 ?jobs?
#
# Output in build/vivado_<design>/: project, bitstream (<name>.bit), XSA (<name>.xsa),
# utilization and timing reports.
# Requirements: Vivado 2025.2 and the Kria K26 SOM board files (xilinx.com:kv260_som:part0:1.4).
# The HLS cores are taken from hls/ip/*.zip (exported by Vitis HLS 2025.2, see README).
# A rebuilt bitstream is functionally equivalent but not byte-identical to the measured one:
# use the files of the GitHub release to repeat the measurements exactly.

if {$argc < 1} {
    error "usage: vivado -mode batch -source build.tcl -tclargs p1|p4 ?jobs?"
}
set design [lindex $argv 0]
set jobs   [expr {$argc > 1 ? [lindex $argv 1] : 4}]

set here [file dirname [file normalize [info script]]]
set root [file dirname $here]

switch -- $design {
    p1 { set ipzip xilinx_com_hls_microbench_1_0.zip;  set name kv260_microbench }
    p4 { set ipzip user_org_hls_microbench_p4_1_0.zip; set name kv260_microbench_p4 }
    default { error "unknown design '$design' (use p1 or p4)" }
}

set proj $root/build/vivado_$design
create_project $name $proj -part xck26-sfvc784-2LV-c -force
set_property board_part xilinx.com:kv260_som:part0:1.4 [current_project]

# IP repository with the exported HLS core
file mkdir $proj/ip_repo
update_ip_catalog -add_ip $root/hls/ip/$ipzip -repo_path $proj/ip_repo
set_property ip_repo_paths [list $proj/ip_repo] [current_project]
update_ip_catalog -rebuild

# block design (copied into the project) and HDL wrapper
import_files -norecurse $here/$design/design_1/design_1.bd
set bd [get_files design_1.bd]
generate_target all $bd
add_files -norecurse [make_wrapper -files $bd -top]
set_property top design_1_wrapper [current_fileset]
update_compile_order -fileset sources_1

# synthesis, implementation, bitstream
launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
    error "implementation of $design did not complete"
}

open_run impl_1
report_utilization -hierarchical -hierarchical_depth 2 -file $proj/utilization_$design.txt
report_timing_summary -max_paths 5 -file $proj/timing_summary_$design.txt
write_hw_platform -fixed -include_bit -force $proj/$name.xsa
file copy -force [lindex [glob $proj/$name.runs/impl_1/*.bit] 0] $proj/$name.bit
puts "Done: $proj/$name.bit  $proj/$name.xsa"
